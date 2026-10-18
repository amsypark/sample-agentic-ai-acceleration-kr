variable "project" {
  description = "Name prefix for the guardrail, matching the rest of the stack."
  type        = string
}

variable "content_filter_strength" {
  description = "Strength applied to every content filter, input and output (NONE/LOW/MEDIUM/HIGH)."
  type        = string
  default     = "HIGH"
}

variable "prompt_attack_strength" {
  description = "Input strength of the PROMPT_ATTACK filter. MEDIUM blocks MEDIUM and HIGH confidence: on the Standard tier that stopped 10 of 10 sample attacks and 0 of 12 ordinary instructions (2026-10-18)."
  type        = string
  default     = "MEDIUM"
}

variable "tier" {
  description = "Safeguard tier: STANDARD (multilingual, Korean included; routed within the region's geography by a guardrail profile) or CLASSIC (English, French and Spanish only)."
  type        = string
  default     = "STANDARD"
  validation {
    condition     = contains(["STANDARD", "CLASSIC"], var.tier)
    error_message = "tier must be STANDARD or CLASSIC."
  }
}

variable "region" {
  description = "Region the guardrail lives in; picks its guardrail profile."
  type        = string
}
