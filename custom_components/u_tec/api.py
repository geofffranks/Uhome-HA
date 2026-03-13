"""API for Uhome bound to Home Assistant OAuth."""

import logging

from aiohttp import ClientSession, web

from homeassistant.components import webhook
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow, network
from homeassistant.helpers.network import NoURLAvailableError
from utec_py.api import AbstractAuth, UHomeApi
from utec_py.exceptions import ApiError, UHomeError, ValidationError

from .const import DOMAIN, WEBHOOK_HANDLER, WEBHOOK_ID_PREFIX

_LOGGER = logging.getLogger(__name__)


class AsyncConfigEntryAuth(AbstractAuth):
    """Provide Uhome Oauth2 authentication tied to an OAuth2 based config entry."""

    def __init__(
        self,
        websession: ClientSession,
        oauth_session: config_entry_oauth2_flow.OAuth2Session,
    ) -> None:
        """Initialize Oauth2 auth."""
        super().__init__(websession)
        self._oauth_session = oauth_session

    async def async_get_access_token(self) -> str:
        """Return a valid access token."""
        await self._oauth_session.async_ensure_token_valid()
        return self._oauth_session.token["access_token"]


class AsyncPushUpdateHandler:
    """Handle webhook registration and processing for Uhome API."""

    def __init__(self, hass: HomeAssistant, api: UHomeApi, entry_id: str) -> None:
        """Initialize the webhook handler."""
        self.hass = hass
        self.entry_id = entry_id
        self.webhook_id = f"{WEBHOOK_ID_PREFIX}{entry_id}"
        self.webhook_url = None
        self._unregister_webhook = None
        self.api = api

    async def async_register_webhook(self, auth_data) -> bool:
        """Register webhook with Home Assistant and the Uhome API."""

        # Try multiple URL resolution strategies so that Nabu Casa,
        # self-hosted SSL, and plain local-network setups all work:
        #   1. External URL (custom domain / DuckDNS / etc.)
        #   2. Cloud URL (Nabu Casa / Home Assistant Cloud)
        #   3. Internal URL as a last resort (only useful if U-Tec's servers
        #      happen to be on the same LAN, which is never true — but we log
        #      a clear warning so users know push updates won't work)
        external_url = None

        for allow_internal, allow_ip, prefer_cloud in [
            (False, False, False),   # pure external first
            (False, False, True),    # cloud / Nabu Casa
            (True, True, False),     # internal fallback (won't work for push, but won't crash)
        ]:
            try:
                external_url = network.get_url(
                    self.hass,
                    allow_internal=allow_internal,
                    allow_ip=allow_ip,
                    prefer_cloud=prefer_cloud,
                )
                if external_url:
                    _LOGGER.debug("Resolved webhook base URL: %s (internal=%s, cloud=%s)",
                                  external_url, allow_internal, prefer_cloud)
                    break
            except NoURLAvailableError:
                continue

        if not external_url:
            _LOGGER.error(
                "No external URL available for push notifications. "
                "Configure an external URL in Settings → System → Network, "
                "or enable Home Assistant Cloud (Nabu Casa)."
            )
            return False

        # Generate the full webhook URL
        webhook_url = webhook.async_generate_url(self.hass, self.webhook_id)

        # Warn if the resolved webhook URL is a local/IP address since
        # U-Tec's cloud servers will never be able to reach it.
        if any(local in webhook_url for local in ("192.168.", "10.", "172.", "homeassistant.local", "localhost", "127.0.")):
            _LOGGER.warning(
                "Webhook URL %s appears to be a local address. "
                "U-Tec's servers cannot reach it — push state updates will not work. "
                "Set up Nabu Casa or an externally-reachable URL.",
                webhook_url,
            )

        # Register the webhook with the U-Tec API
        try:
            _LOGGER.debug("Registering webhook URL: %s", webhook_url)
            result = await self.api.set_push_status(webhook_url)
            _LOGGER.debug("Webhook registration result: %s", result)
        except ApiError as err:
            _LOGGER.error("Failed to register webhook with U-Tec API: %s", err)
            return False
        else:
            # Register HA-side webhook handler
            self._unregister_webhook = webhook.async_register(
                self.hass,
                DOMAIN,
                WEBHOOK_HANDLER,
                self.webhook_id,
                self._handle_webhook,
            )
            self.webhook_url = webhook_url
            return True

    async def unregister_webhook(self) -> None:
        """Unregister the webhook."""
        if self._unregister_webhook:
            webhook.async_unregister(self.hass, self.webhook_id)
            self._unregister_webhook = None
            _LOGGER.debug("Unregistered webhook %s", self.webhook_id)

    async def _handle_webhook(
        self, hass: HomeAssistant, webhook_id, request
    ) -> web.Response | None:
        """Handle webhook callback."""
        try:
            if request.method != "POST":
                _LOGGER.error("Unsupported method: %s", request.method)
                return web.Response(status=405)

            # Gracefully handle malformed JSON bodies instead of letting
            # the exception propagate uncaught (which caused a 500 to be returned
            # to U-Tec's servers, potentially causing them to stop sending events).
            try:
                data = await request.json()
            except Exception as json_err:  # noqa: BLE001
                _LOGGER.error("Failed to parse webhook JSON: %s", json_err)
                return web.Response(status=400)

            _LOGGER.debug("Received webhook data: %s", data)

            if self.entry_id not in hass.data.get(DOMAIN, {}):
                _LOGGER.error("Unknown entry_id in webhook: %s", self.entry_id)
                return web.Response(status=404)

            coordinator = hass.data[DOMAIN][self.entry_id]["coordinator"]

            # Process the device update (coordinator handles all shape variants)
            await coordinator.update_push_data(data)

        except UHomeError as err:
            _LOGGER.error("Error processing webhook: %s", err)
            return web.json_response({"success": False, "error": str(err)}, status=400)
        except Exception as err:  # noqa: BLE001
            # Catch-all so unexpected errors return a proper HTTP
            # response rather than letting aiohttp generate a 500, which would
            # appear in HA logs as an unhandled exception and potentially cause
            # U-Tec's servers to unregister our webhook endpoint.
            _LOGGER.exception("Unexpected error processing webhook: %s", err)
            return web.json_response({"success": False, "error": "Internal error"}, status=500)
        else:
            return web.json_response({"success": True})
