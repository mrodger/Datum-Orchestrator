-- Migration 001: Add drone cost/usage and extraction token tracking to orchestration_runs
-- Safe to run on existing deployments — all columns are nullable with no defaults required.

ALTER TABLE orchestration_runs
    ADD COLUMN IF NOT EXISTS cost_usd                FLOAT,
    ADD COLUMN IF NOT EXISTS tokens_input            INT,
    ADD COLUMN IF NOT EXISTS tokens_output           INT,
    ADD COLUMN IF NOT EXISTS num_turns               INT,
    ADD COLUMN IF NOT EXISTS duration_ms             INT,
    ADD COLUMN IF NOT EXISTS extraction_tokens_input  INT,
    ADD COLUMN IF NOT EXISTS extraction_tokens_output INT;
