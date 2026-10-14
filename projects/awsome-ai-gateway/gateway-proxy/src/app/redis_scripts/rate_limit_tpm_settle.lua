-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- TPM 사후 정산 — 단일 스코프 (USER, TEAM 또는 GLOBAL)
--
-- 예약(rate_limit_tpm_check.lua)이 토큰을 넣은 **그 버킷**에만 차액을 반영한다. 호출자
-- (rate_limit_service.settle_tpm)는 예약할 때 기록해 둔 세 키와 버킷 번호를 그대로 넘긴다.
-- 예약하지 않은 스코프는 호출 자체가 없다.
--
-- Redis Cluster CROSSSLOT(deepdive Q50): 세 키는 같은 hash tag({scope:id:model}) = 같은 슬롯이다.
-- 다른 스코프의 키와 한 EVAL 에 섞지 않는다 — 호출자가 스코프마다 한 번씩 부른다.
--
-- KEYS[1] = {scope:id:model}:tpm:cur      (현재 버킷 토큰 합)
-- KEYS[2] = {scope:id:model}:tpm:prev     (직전 버킷 토큰 합)
-- KEYS[3] = {scope:id:model}:tpm:window   (cur 가 담고 있는 버킷 번호)
-- ARGV[1] = adjustment (tokens, actual - reserved. 음수 = 환불)
-- ARGV[2] = 예약한 버킷 번호 (floor(now / window_sec), 예약 시각)
-- ARGV[3] = TTL (rate_limit_tpm_check.lua 의 window_sec * 2 + 60)
--
-- 버킷은 분마다 회전한다(cur → prev). 그래서 예약한 버킷이 지금 어디에 있는지를 window 마커로 본다:
--   * 마커 == 예약 버킷     → 아직 cur 다. cur 를 조정한다.
--   * 마커 == 예약 버킷 + 1 → 한 번 회전해 prev 로 옮겨졌다. prev 를 조정한다(가중 추정에 아직 들어간다).
--   * 그 밖(마커 없음·두 번 이상 회전·키 만료) → 예약한 버킷은 어떤 추정에도 더는 들어가지 않는다.
--     아무것도 쓰지 않는다. 지금의 cur 를 조정하면 다른 분의 카운터를 움직이는 것이다.
-- 조정할 키가 없으면 만들지 않는다. 있으면 INCRBY 하고 TTL 은 그대로 둔다(없으면 ARGV[3] 으로 건다 —
-- 이 수정 이전 코드가 남긴 키). 결과가 음수면 0 으로 둔다(TTL 유지): 버킷의 토큰 합은 음수일 수 없고,
-- 음수는 그 분 동안 남의 한도를 빌려 준다.
--
-- Returns: 1 = 조정함, 0 = 예약한 버킷이 없어 건너뜀

local adj = tonumber(ARGV[1])
local bucket = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local marker = tonumber(redis.call('GET', KEYS[3]) or '-1')

local target
if marker == bucket then
    target = KEYS[1]
elseif marker == bucket + 1 then
    target = KEYS[2]
else
    return 0
end

if redis.call('EXISTS', target) == 0 then
    return 0
end
local v = redis.call('INCRBY', target, adj)
if v < 0 then
    redis.call('SET', target, '0', 'KEEPTTL')
end
if redis.call('TTL', target) < 0 then
    redis.call('EXPIRE', target, ttl)
end
return 1
