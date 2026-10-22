"""Follow only the admission we submitted; never infer a reply from session activity."""
import asyncio
import time


async def wait_reply(client, session_id, admission_id, seconds, *, sleep=asyncio.sleep, clock=time.monotonic):
    deadline = clock() + min(300, max(0, seconds))
    while True:
        receipt = await client.rpc("prompt.receipt", session_id=session_id, admission_id=admission_id, include_result=True)
        if receipt.get("status") == "terminal":
            result = receipt.get("result") or {}
            text = result.get("final_response") or ""
            if receipt.get("outcome") == "completed" and isinstance(text, str) and text.strip() and not result.get("failed"):
                return {"reply_state": "completed", "final_reply": text.strip(), "admission_id": admission_id}
            return {"reply_state": receipt.get("outcome") or "unknown", "final_reply": "", "admission_id": admission_id}
        if receipt.get("status") not in {"queued", "started", "running"}:
            return {"reply_state": "unknown", "final_reply": "", "admission_id": admission_id}
        if clock() >= deadline:
            return {"reply_state": "pending", "final_reply": "", "admission_id": admission_id}
        await sleep(min(1, max(0, deadline - clock())))
