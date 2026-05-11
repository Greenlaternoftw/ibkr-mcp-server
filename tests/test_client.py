"""Tests for IBKR client functionality."""

import pytest
from unittest.mock import AsyncMock, patch

from ibkr_mcp_server.client import IBKRClient


class TestIBKRClient:
    """Test IBKR client functionality."""

    @pytest.mark.asyncio
    async def test_account_switching(self, ibkr_client_mock):
        result = await ibkr_client_mock.switch_account('DU7654321')
        assert result['success'] is True
        assert ibkr_client_mock.current_account == 'DU7654321'

        result = await ibkr_client_mock.switch_account('INVALID')
        assert result['success'] is False
        assert ibkr_client_mock.current_account == 'DU7654321'

    @pytest.mark.asyncio
    async def test_get_accounts(self, ibkr_client_mock):
        accounts = await ibkr_client_mock.get_accounts()
        assert accounts['current_account'] == 'DU1234567'
        assert 'DU1234567' in accounts['available_accounts']
        assert 'DU7654321' in accounts['available_accounts']

    def test_is_connected(self, ibkr_client_mock):
        ibkr_client_mock.ib.isConnected.return_value = True
        assert ibkr_client_mock.is_connected() is True

        ibkr_client_mock._connected = False
        assert ibkr_client_mock.is_connected() is False

    @pytest.mark.asyncio
    async def test_get_portfolio_not_connected(self):
        client = IBKRClient()
        client._connected = False

        with patch.object(client, '_ensure_connected', AsyncMock(return_value=False)):
            with pytest.raises(RuntimeError, match="Not connected to IBKR"):
                await client.get_portfolio()
