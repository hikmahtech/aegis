from aegis_comms.adapters.base import CardSpec, DeliveryRef, SendResult


def test_delivery_ref_roundtrips_telegram():
    ref = DeliveryRef(adapter="telegram", data={"chat_id": -100, "message_id": 5})
    assert ref.to_dict() == {"adapter": "telegram", "chat_id": -100, "message_id": 5}
    assert DeliveryRef.from_dict(ref.to_dict()) == ref


def test_delivery_ref_slack_no_collision():
    # Slack's natural payload has a "channel" key ("C1") that previously
    # collided with the old discriminator field also named "channel".
    # With adapter= as the discriminator the round-trip must be the identity.
    ref = DeliveryRef(adapter="slack", data={"channel": "C1", "ts": "1.2"})
    assert ref.to_dict() == {"adapter": "slack", "channel": "C1", "ts": "1.2"}
    assert DeliveryRef.from_dict(ref.to_dict()) == ref


def test_send_result_defaults():
    r = SendResult(ok=True, ref=DeliveryRef(adapter="slack", data={"channel": "C1", "ts": "1.2"}))
    assert r.ok and r.used_html is True and r.ref.data["ts"] == "1.2"


def test_card_spec_holds_neutral_fields():
    c = CardSpec(interaction_id="i1", agent_id="sebas", kind="approval", prompt="ok?", options=None)
    assert c.kind == "approval" and c.interaction_id == "i1"
