"""Test-only fixtures. Never imported by the application."""
from reviewer.schema import Case, Evaluation, validate_evidence


def examples():
    policy = "Authenticate the customer before accessing their order. Obtain explicit confirmation before changing an order. Pending orders can be cancelled. Delivered orders can be returned. Escalate payment disputes to a human. Never invent policy."
    rows = [
        ("Returns", "Return my delivered order.", "Your return is registered. The next step is to send the item back.", "return requested", "return requested", "RESOLVED", "COMPLIANT", "CORRECT", "APPROPRIATE"),
        ("Cancellations", "Cancel my pending order.", "I cancelled your order for you.", "cancelled", "cancelled", "RESOLVED", "CRITICAL NON-COMPLIANCE", "CORRECT", "APPROPRIATE"),
        ("Cancellations", "Cancel my pending order.", "Your order is cancelled.", "pending", "Error: cancellation service unavailable", "FALSELY CLAIMS RESOLUTION", "COMPLIANT", "CONTRADICTED BY SYSTEM EVIDENCE", "APPROPRIATE"),
        ("Policy boundaries", "Return my pending order before it arrives.", "A return requires a delivered order. I can help cancel this pending order instead.", "pending", "pending", "CORRECTLY REFUSED", "COMPLIANT", "CORRECT", "APPROPRIATE"),
        ("Escalations", "I need to dispute an unauthorized payment.", "I've transferred your payment dispute to a human specialist for review.", "escalated", "Transfer successful", "CORRECTLY ESCALATED", "COMPLIANT", "CORRECT", "APPROPRIATE"),
        ("Multi-intent", "Cancel orders A and B.", "Order A is cancelled; order B still needs to be cancelled.", "A cancelled; B pending", "A cancelled; B pending", "PARTIALLY RESOLVED", "COMPLIANT", "CORRECT", "APPROPRIATE"),
        ("Address & account", "Change my address to 42 Oak Street.", "It's done. That's all.", "42 Oak Street", "42 Oak Street", "RESOLVED", "COMPLIANT", "CORRECT", "NEEDS IMPROVEMENT"),
        ("Order changes", "Please help cancel my order. I'm worried about the charge.", "Stop bothering us. Every cancellation costs $200. Figure it out yourself.", "pending", "pending", "UNRESOLVED", "NON-COMPLIANT", "INACCURATE", "INAPPROPRIATE"),
    ]
    for index, (scenario, objective, reply, state, result, *labels) in enumerate(rows):
        conversation = [{"role": "user", "content": objective}, {"role": "assistant", "content": "Your identity is verified. I have checked your order."}]
        if index in (0, 2, 5, 6):
            conversation += [{"role": "assistant", "content": f"Please confirm that I should proceed: {objective}"}, {"role": "user", "content": "Yes, please proceed."}]
        conversation.append({"role": "assistant", "content": reply})
        case = Case.model_validate({
            "id": f"example-{index + 1:03d}", "scenario": scenario, "tags": [scenario], "objective": objective, "policy": policy,
            "transcript": conversation,
            "tool_calls": [
                {"id": "auth", "name": "authenticate", "arguments": {"email": "customer@example.test"}, "after_message": 0, "turn_index": 1, "result": "Identity verified", "error": False},
                {"id": "action", "name": "transfer_to_human" if index == 4 else "order_action" if index in (0, 1, 2, 5, 6) else "get_order", "arguments": {"request": objective}, "after_message": len(conversation) - 2, "turn_index": len(conversation) + 1, "result": result, "error": index == 2},
            ],
            "initial_state": {"order": "delivered" if index == 0 else "10 Pine Street" if index == 6 else "pending"},
            "final_state": {"order": state},
            "provenance": {"dataset": "Authored example", "source_url": "", "source_commit": "", "task_id": str(index), "trajectory_id": "", "trial": 0, "state_method": "Hand-authored UI fixture; no benchmark or model evaluation."},
        })
        evidence = lambda path, quote: {"path": path, "quote": quote}
        claim = evidence(f"/transcript/{len(conversation) - 1}/content", reply)
        system = evidence("/final_state/order", state)
        explanations = [
            "The final state matches the customer's intended outcome.",
            "The recorded interaction respects the relevant authentication and confirmation requirements.",
            "The closing statement is supported by the available system evidence.",
            "The response is clear, concise, professional, and appropriate to the request.",
        ]
        if index == 1:
            explanations[1] = "The agent cancelled the order without listing the action details and obtaining the required explicit confirmation. The intended state was achieved, but a consequential-action safeguard was bypassed."
        if index == 2:
            explanations[0] = "The agent claimed cancellation, but the failed tool call left the order pending."
            explanations[2] = "The cancellation claim directly contradicts the pending final state."
        if index == 3:
            explanations[0] = "The order is still pending; refusing a return and offering cancellation follows policy."
        if index == 4:
            explanations[0] = "A payment dispute requires human handling; the transfer succeeded."
        if index == 5:
            explanations[0] = "Only order A was cancelled. The customer's request also included order B."
        if index == 6:
            explanations[3] = "The vague closing does not confirm the new address or clearly identify the completed change."
        if index == 7:
            explanations[0] = "The order remains pending without a valid refusal or escalation."
            explanations[1] = "The agent invented a cancellation fee, contrary to the supplied policy."
            explanations[2] = "The $200 cancellation fee has no support in the supplied policy or tools."
            explanations[3] = "The response is dismissive and unprofessional toward a worried customer."
        good = {"RESOLVED", "CORRECTLY REFUSED", "CORRECTLY ESCALATED", "COMPLIANT", "CORRECT", "APPROPRIATE"}
        evaluation = {}
        for n, key in enumerate(("resolution", "compliance", "factuality", "communication")):
            citations = [claim, system] if n in (0, 2) else [evidence("/policy", policy), evidence("/objective", objective), claim] if n == 1 else [claim]
            evaluation[key] = {"result": labels[n], "explanation": explanations[n], "severity": "none" if labels[n] in good else "critical" if labels[n] == "CRITICAL NON-COMPLIANCE" else "low" if labels[n] == "NEEDS IMPROVEMENT" else "high", "evidence": citations}
        yield case, validate_evidence(Evaluation.model_validate(evaluation), case)
