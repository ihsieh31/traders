# Real Alpaca Paper crash/restart recovery gate

**Status: PENDING.** This procedure is an operator-run acceptance test. A fake
broker, unit test, or local dry run does not change this status. Run it only
during regular US market hours against a dedicated Alpaca Paper account.

## Preconditions

1. Use a dedicated Paper account with no unrelated positions or orders. Confirm
   the configured endpoint is Alpaca Paper and keep credentials out of notes,
   terminal captures, and evidence files.
2. Use the normal `long-run` production entry point, normal account binding,
   market clock, screening/Top20 gate, READY `TradeIntent`, quote checks,
   safety guard, broker reconciliation, and configured low notional. Do not
   force a symbol or signal, disable a guard, change the clock, or call the
   broker SDK directly.
3. Confirm the production screen can produce a valid Top20 under the current
   deterministic eligibility rules. If it cannot, stop. Do not bypass the
   screen to manufacture a recovery order.
4. Start shortly before the configured session target and ensure the order
   submission can happen before target plus the 30-minute session grace.

## Procedure

1. Start the ordinary production command in a debugger, with the same
   environment and settings used for a normal Paper observation:

   ```bash
   python -m pdb -m cli.main long-run --backend traders --duration-days 1
   ```

   Use the normal prompts to select/resume the intended Paper observation.
   Keep the configured notional minimal and within existing risk limits.

2. Set a source breakpoint in
   [`tradingagents/execution/dispatch.py`](../tradingagents/execution/dispatch.py)
   on the first local statement immediately after the actual
   `broker.submit_order(...)` call returns. There are separate protected and
   unprotected branches; use the branch reached by the normal READY intent.
   Continue until it stops. Before the local completion transition, record
   `decision_id`, `client_oid`, `resp.id`, and the local primary-order status.
   Expected local status is `SUBMITTING`; the debugger stop proves the submit
   call returned once.

3. From a second terminal, terminate that process with `kill -9 <pid>` while it
   is stopped. Do not use a graceful shutdown. Preserve its run directory and
   execution database. Query the dedicated Paper account using the ordinary
   broker UI/API and record the order matching the recorded client order ID,
   including broker order ID and status. Do not include credentials in the
   record.

4. Restart the same long-run observation through the ordinary CLI resume flow.
   Let its normal `startup_recover()` and reconciliation finish. Keep the
   submit breakpoint enabled: it must not be reached for this decision. The
   recovery evidence must show a lookup by the same deterministic
   `client_order_id`, adoption of the existing broker order, and a CLEAN
   execution account state.

5. Re-query the Paper account and inspect the execution DB. Verify the same
   `decision_id` and `client_order_id` remain associated with exactly one
   primary broker order and one local primary row; there was no second POST.
   Record the final broker status and recovery result.

## Evidence record

Save a redacted evidence note containing:

- Date/time and confirmation that regular market hours were open.
- Run ID and the same `decision_id` before and after restart.
- Deterministic `client_order_id` before and after restart.
- Before-crash local state (`SUBMITTING`) and broker order ID/status.
- First-run submit breakpoint count (`1`) and restart submit breakpoint count
  (`0`); include the debugger/source location as observable POST evidence.
- After-restart local state, lookup/adoption result, and
  `startup_recover()`/reconciliation result (`success: true`, CLEAN).
- Final broker status and counts: exactly one broker primary order and one
  local primary order.

Do not attach API keys, secrets, account credentials, or unredacted environment
output. If the order outcome, lookup, account state, or counts are uncertain,
record the gate as FAILED/PENDING and leave the durable state for operator
reconciliation. Never delete or recreate the order to make the evidence pass.

After one complete successful run, update the status only with the recorded
evidence and review date. Until then, keep the repository status **PENDING**.
