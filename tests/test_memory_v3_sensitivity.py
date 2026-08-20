from mapi_core.memory.sensitivity import classify_memory_sensitivity


def test_security_policy_description_is_not_a_secret() -> None:
    result = classify_memory_sensitivity(
        "Opis zasad bezpiecznego przechowywania hasel, tokenow i kluczy API."
    )
    assert result["sensitivity_class"] == "internal"
    assert result["capture_allowed"] is True


def test_structured_real_credential_is_blocked_without_value_leak() -> None:
    secret = "Abcd1234!real-value"
    result = classify_memory_sensitivity(f"api_key={secret}")
    assert result["sensitivity_class"] == "credential_secret"
    assert result["capture_allowed"] is False
    assert secret not in repr(result)


def test_public_price_is_public_only_with_explicit_signal() -> None:
    public = classify_memory_sensitivity("Publiczny cennik produktu: 99 PLN", metadata={"visibility_scope": "public"})
    internal = classify_memory_sensitivity("Oferta produktu: 99 PLN")
    assert public["sensitivity_class"] == "public"
    assert internal["sensitivity_class"] == "internal"


def test_health_financial_and_personal_need_hard_context() -> None:
    assert classify_memory_sensitivity("patient diagnosis: X")["sensitivity_class"] == "health_sensitive"
    assert classify_memory_sensitivity("private bank account balance")["sensitivity_class"] == "financial_sensitive"
    assert classify_memory_sensitivity("personal phone: hidden")["sensitivity_class"] == "personal"
