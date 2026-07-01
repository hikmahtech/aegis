from aegis_comms.__main__ import CardDeliveryRequest


def test_card_delivery_request_accepts_allow_hint():
    req = CardDeliveryRequest(interaction_id="X", kind="choice", allow_hint=True)
    assert req.allow_hint is True


def test_card_delivery_request_defaults_false():
    req = CardDeliveryRequest(interaction_id="X", kind="choice")
    assert req.allow_hint is False
