import pytest
from Connection import connection
from ibapi.contract import Contract

conn = connection.Connection(event_queue=None)


def test_create_contract_valid():
    contract = conn.create_contract("AAPL")
    assert isinstance(contract, Contract)
    assert contract.symbol == "AAPL"
    assert contract.secType == "STK"
    assert contract.currency == "USD"
    assert contract.exchange == "SMART"


def test_crearte_contract_white_space():
    with pytest.raises(ValueError, match="Symbol must be a non-empty string."):
        conn.create_contract("  ")


def test_create_contract_invalid_type():
    with pytest.raises(ValueError, match="Symbol must be a non-empty string."):
        conn.create_contract(123)


def test_create_contract_invalid_secType():
    with pytest.raises(ValueError, match="secType must be a STK."):
        conn.create_contract("123",secType="123")


def test_create_contract_invalid_currency():
    with pytest.raises(ValueError, match="currency must be USD."):
        conn.create_contract("123",currency="123")


def test_create_contract_invalid_exchange():
    with pytest.raises(ValueError, match="exchange must be SMART"):
        conn.create_contract("123", exchange="123")
