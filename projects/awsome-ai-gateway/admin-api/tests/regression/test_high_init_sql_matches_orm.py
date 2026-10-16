# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``db/init`` 로만 만든 스키마가 **모든 ORM 컬럼**을 갖는지 실 PG 로 전수 대조한다.

왜 이 검사가 필요한가
---------------------
ORM 이 선언한 컬럼은 그 테이블의 **모든 SELECT/INSERT** 에 들어간다. 그래서 init SQL 에
컬럼이 없으면 그 기능만 죽는 것이 아니라, 그 테이블을 쓰는 **경로 전체**가
``UndefinedColumn`` 으로 죽는다. 마이그레이션에만 추가하고 init SQL 을 빠뜨리는 것은
코드를 읽어서는 드러나지 않고, 마이그레이션이 끝까지 돌아간 환경에서는 아무 증상도 없다.

실제로 그 상태로 배포된 것이 세 번 있었다:

  * ``model.model_aliases.allowed_clients`` — 앱별 모델 allow-list 게이트가 무력화됐다.
  * ``usage.usage_logs.client`` — cost-recorder-worker 의 배치 INSERT 가 **전부** 실패해
    사용량 행이 하나도 쌓이지 않는다.
  * ``model.routing_profiles`` / ``public.system_settings`` — 테이블 자체가 없었다.

세 번 같은 방식으로 새어 나갔으므로, 개별 사례를 하나씩 못 박는 대신 **차집합을 기계적으로
0 으로 유지**한다. 새 마이그레이션을 넣고 init SQL 을 빠뜨리면 여기서 잡힌다.

어디에 영향이 있나
------------------
compose, 로컬 개발 스택, 그리고 alembic 이 아직 그 리비전에 닿지 않은 모든 환경. CI 의
``pg-proof`` 잡은 init SQL 을 먼저 적용하고 그 위에 alembic 을 돌리므로, init SQL 이
반쪽이어도 최종 스키마는 정상이 된다 — 즉 **이 검사 없이는 CI 가 이 결함을 통과시킨다.**

⚠️ 자기 소유 스크래치 DB 를 만든다. 공용 proof DB 에 init SQL 만 적용해 비교하면 이미
   마이그레이션이 올라간 스키마와 섞여 차집합이 항상 0 으로 보인다(공허한 통과).

⚠️ init SQL 은 ``ON_ERROR_STOP=1`` 로 적용한다. 실패를 무시하면 반쪽 스키마를 정상으로
   오판한다 — 실제로 ALTER 문을 파일 중간(대상 테이블 생성 전)에 넣었다가 생성 컬럼이
   237개에서 84개로 줄어든 것을 이 방식으로 잡았다.

실행:
    PROOF_DSN=postgresql+asyncpg://postgres:...@127.0.0.1:55432/gwproof \\
      pytest tests/regression/test_high_init_sql_matches_orm.py
"""

from __future__ import annotations

import importlib
import os
import pkgutil
from pathlib import Path

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")

_OWNED_SUFFIX = "_initonly"
_PROJECT = Path(__file__).resolve().parents[3]
_INIT_DIR = _PROJECT / "db" / "init"

#: 대조 대상 스키마. ``notification`` 등 ORM 이 없는 스키마는 뺀다.
_SCHEMAS = ("auth", "budget", "model", "usage", "audit", "public")

pytestmark = pytest.mark.skipif(
    not PROOF_DSN,
    reason=(
        "PROOF_DSN 미설정 — 실 PG 필요. 'init SQL 만으로 만든 스키마' 는 실제로 적용해 봐야 "
        "알 수 있다(파일을 정규식으로 읽는 것은 DO 블록·조건부 ALTER 앞에서 무의미하다)."
    ),
)


def _split(dsn: str) -> tuple[str, str]:
    prefix, _, name = dsn.rpartition("/")
    return prefix, name


def _asyncpg(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


def _orm_columns() -> dict[tuple[str, str], set[str]]:
    """이 서비스의 ORM 이 선언한 ``(schema, table) -> {column}``.

    ``Base.metadata.sorted_tables`` 를 쓰지 않는다 — 이 서비스의 모델은 다른 서비스가
    소유한 테이블을 FK 로 참조하므로 정렬 과정에서 ``NoReferencedTableError`` 가 난다.
    등록된 ``__table__`` 을 모듈에서 직접 모은다.
    """
    import app.models as models_pkg

    tables: dict[tuple[str, str], set[str]] = {}
    for mod_info in pkgutil.iter_modules(models_pkg.__path__):
        mod = importlib.import_module(f"app.models.{mod_info.name}")
        for name in dir(mod):
            table = getattr(getattr(mod, name), "__table__", None)
            if table is None or not hasattr(table, "columns"):
                continue
            key = (table.schema or "public", table.name)
            tables[key] = {c.name for c in table.columns}
    assert tables, "ORM 테이블을 하나도 찾지 못했다 — 이 검사가 공허하다"
    return tables


@pytest.fixture(scope="module")
async def init_only_schema() -> dict[tuple[str, str], set[str]]:
    """``db/init`` 만 적용한 스크래치 DB 의 ``(schema, table) -> {column}``."""
    asyncpg = pytest.importorskip("asyncpg")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, name = _split(PROOF_DSN)
    owned = f"{name}{_OWNED_SUFFIX}"
    # 파괴적 DDL 을 도는 DB 이므로 이름으로 한 번 더 잠근다.
    assert any(t in owned for t in ("proof", "test", "scratch", "tmp")), owned

    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
            await c.execute(text(f'CREATE DATABASE "{owned}"'))
    finally:
        await admin.dispose()

    url = f"{prefix}/{owned}"
    conn = await asyncpg.connect(_asyncpg(url))
    failures: list[str] = []
    try:
        for f in sorted(_INIT_DIR.glob("*.sql")):
            sql = f.read_text(encoding="utf-8")
            try:
                await conn.execute(sql)
            except Exception as e:  # noqa: BLE001
                # ⚠️ 실패를 기록해 두고 아래에서 단정한다. 조용히 넘기면 반쪽 스키마를
                #    정상으로 오판한다.
                failures.append(f"{f.name}: {type(e).__name__}: {str(e)[:160]}")

        rows = await conn.fetch(
            "SELECT table_schema, table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = ANY($1::text[])",
            list(_SCHEMAS),
        )
    finally:
        await conn.close()

    schema: dict[tuple[str, str], set[str]] = {}
    for r in rows:
        schema.setdefault((r["table_schema"], r["table_name"]), set()).add(r["column_name"])

    yield {"schema": schema, "failures": failures, "url": url}

    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
    finally:
        await admin.dispose()


#: ``db/init`` 이 이 환경에서 낼 수 있는, 무해한 실패. 그 밖의 실패는 단정에서 걸린다.
#: ``08_create_chat_reader.sql`` 은 ``\c gateway`` 로 이름이 정해진 DB 에 접속하므로
#: 스크래치 DB 에서는 성립하지 않는다 — 역할/권한만 만드는 파일이라 컬럼 대조와 무관하다.
_TOLERATED_INIT_FAILURES = ("08_create_chat_reader.sql",)


async def test_init_sql_applies_without_unexpected_errors(init_only_schema):
    """⚠️ 먼저 이것을 확인한다 — 반쪽 스키마로 아래 대조를 하면 결과가 무의미하다."""
    unexpected = [
        f
        for f in init_only_schema["failures"]
        if not f.startswith(_TOLERATED_INIT_FAILURES)
    ]
    assert unexpected == [], "db/init 적용 중 예상치 못한 실패:\n  " + "\n  ".join(unexpected)


async def test_every_orm_column_exists_in_init_sql(init_only_schema):
    """⚠️ 이 파일의 핵심. 차집합이 0 이어야 한다.

    새 마이그레이션을 넣고 ``db/init`` 을 빠뜨리면 여기서 잡힌다. 실패하면 고치는 방법은
    ``db/init/02_create_tables.sql`` **맨 끝**에 idempotent DDL 을 추가하는 것이다
    (``ADD COLUMN IF NOT EXISTS`` / ``CREATE TABLE IF NOT EXISTS``). 위치가 중요하다 —
    참조 테이블이 만들어진 뒤여야 하고, init SQL 은 한 문장이 실패하면 나머지가 통째로
    적용되지 않는다.
    """
    live = init_only_schema["schema"]
    missing: list[str] = []
    checked = 0
    for (sch, table), columns in sorted(_orm_columns().items()):
        for col in sorted(columns):
            checked += 1
            if col not in live.get((sch, table), set()):
                missing.append(f"{sch}.{table}.{col}")

    assert checked > 100, f"{checked}개만 대조했다 — ORM 수집이 깨졌다(공허한 통과)"
    assert missing == [], (
        f"ORM 이 선언했는데 db/init 에 없는 컬럼 {len(missing)}건 "
        f"(대조 {checked}개):\n  " + "\n  ".join(missing)
    )


async def test_the_comparison_can_actually_fail(init_only_schema):
    """대조군 — 위 검사가 "무엇이든 통과" 가 아님을 보인다.

    실재하지 않는 컬럼을 같은 방식으로 조회하면 반드시 없다고 나와야 한다. 이게 없으면
    ``information_schema`` 조회가 빈 결과를 돌려주는 상황(스키마 이름 오타 등)에서
    위 단정이 조용히 통과한다.
    """
    live = init_only_schema["schema"]
    assert ("auth", "users") in live, "auth.users 조차 못 찾았다 — 조회가 깨졌다"
    assert "column_that_does_not_exist" not in live[("auth", "users")]


#: 이 백엔드가 ``usage.usage_logs`` 에 쥔 테이블 락 모드(트랜잭션이 끝날 때까지 남는다).
_USAGE_LOGS_LOCKS = (
    "SELECT DISTINCT mode FROM pg_locks WHERE pid = pg_backend_pid() "
    "AND locktype = 'relation' AND relation = 'usage.usage_logs'::regclass"
)


async def test_rerunning_init_sql_takes_no_access_exclusive_on_usage_logs(init_only_schema):
    """``db/init`` 은 모든 마이그레이션 Job 에서 alembic 앞에 다시 돈다 — 그때 원장 테이블을 세우면 안 된다.

    컬럼이 이미 있어 no-op 인 ``ADD COLUMN IF NOT EXISTS`` 도 ACCESS EXCLUSIVE 를 잡는다(실 PG16
    측정). 그 락은 읽기까지 막고, 열린 트랜잭션 뒤에서 기다리는 동안 그 테이블의 모든 읽기·쓰기를
    뒤에 줄 세운다. 그래서 usage_logs 의 컬럼 추가는 information_schema 로 존재를 먼저 본다.
    파일마다 한 트랜잭션에서 돌려 쥔 락을 읽고 되돌린다(스크래치 DB, 이미 한 번 적용된 상태 =
    두 번째 Job).
    """
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(_asyncpg(init_only_schema["url"]))
    try:
        held: dict[str, list[str]] = {}
        for f in sorted(_INIT_DIR.glob("*.sql")):
            if f.name.startswith(_TOLERATED_INIT_FAILURES):
                continue
            tx = conn.transaction()
            await tx.start()
            try:
                await conn.execute(f.read_text(encoding="utf-8"))
                held[f.name] = sorted(r["mode"] for r in await conn.fetch(_USAGE_LOGS_LOCKS))
            finally:
                await tx.rollback()
        assert held, "init SQL 을 하나도 다시 돌리지 않았다 — 이 검사가 공허하다"
        offenders = {name: modes for name, modes in held.items() if "AccessExclusiveLock" in modes}
        assert offenders == {}, f"init SQL 재실행이 usage.usage_logs 를 ACCESS EXCLUSIVE 로 잠근다: {offenders}"

        # 대조군 — 같은 조회가 가드 없는 문장의 락은 실제로 잡는다(조회가 공허하지 않다).
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(
                "ALTER TABLE usage.usage_logs ADD COLUMN IF NOT EXISTS context_tier VARCHAR(8)"
            )
            control = {r["mode"] for r in await conn.fetch(_USAGE_LOGS_LOCKS)}
        finally:
            await tx.rollback()
        assert "AccessExclusiveLock" in control, control
    finally:
        await conn.close()
