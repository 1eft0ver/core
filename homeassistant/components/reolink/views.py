"""Reolink Integration views."""

import asyncio
from base64 import urlsafe_b64decode, urlsafe_b64encode
from http import HTTPStatus
import logging
import re

from aiohttp import ClientError, ClientTimeout, web
from reolink_aio.enums import VodRequestType
from reolink_aio.exceptions import ReolinkError

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.media_source import Unresolvable
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.ssl import SSLCipherList

from .util import get_host

_LOGGER = logging.getLogger(__name__)


@callback
def async_generate_playback_proxy_url(
    config_entry_id: str, channel: int, filename: str, stream_res: str, vod_type: str
) -> str:
    """Generate proxy URL for event video."""

    url_format = PlaybackProxyView.url
    return url_format.format(
        config_entry_id=config_entry_id,
        channel=channel,
        filename=urlsafe_b64encode(filename.encode("utf-8")).decode("utf-8"),
        stream_res=stream_res,
        vod_type=vod_type,
    )


class PlaybackProxyView(HomeAssistantView):
    """View to proxy playback video from Reolink."""

    requires_auth = True
    url = (
        "/api/reolink/video"
        "/{config_entry_id}/{channel}/{stream_res}"
        "/{vod_type}/{filename}"
    )
    name = "api:reolink_playback"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize a proxy view."""
        self.hass = hass
        self.session = async_get_clientsession(
            hass,
            verify_ssl=False,
            ssl_cipher=SSLCipherList.INSECURE,
        )
        self._vod_type: str | None = None

    async def get(
        self,
        request: web.Request,
        config_entry_id: str,
        channel: str,
        stream_res: str,
        vod_type: str,
        filename: str,
        retry: int = 2,
    ) -> web.StreamResponse:
        """Get playback proxy video response."""
        retry = retry - 1

        filename_decoded = urlsafe_b64decode(filename.encode("utf-8")).decode("utf-8")
        ch = int(channel)

        # Parse the clip duration from the recording file name (Rec<ai>_DST
        # <date>_<start HHMMSS>_<end HHMMSS>_...) so ffmpeg can stop exactly at
        # the end instead of waiting for the stream to stall, and so the remuxed
        # MP4 has a correct total duration (seekable progress bar).
        clip_seconds: int | None = None
        _dur_m = re.search(
            r"Rec\w{3}(?:_DST|_)\d{8}_(\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_",
            filename_decoded,
        )
        if _dur_m:
            g = [int(x) for x in _dur_m.groups()]
            start_s = g[0] * 3600 + g[1] * 60 + g[2]
            end_s = g[3] * 3600 + g[4] * 60 + g[5]
            diff = end_s - start_s
            if diff < 0:
                diff += 86400
            if 0 < diff <= 1800:
                clip_seconds = diff
        try:
            host = get_host(self.hass, config_entry_id)
        except Unresolvable:
            err_str = (
                "Reolink playback proxy could not find"
                f" config entry id: {config_entry_id}"
            )
            _LOGGER.warning(err_str)
            return web.Response(body=err_str, status=HTTPStatus.BAD_REQUEST)

        try:
            _mime_type, reolink_url = await host.api.get_vod_source(
                ch, filename_decoded, stream_res, VodRequestType(vod_type)
            )
        except ReolinkError as err:
            _LOGGER.warning("Reolink playback proxy error: %s", str(err))
            return web.Response(body=str(err), status=HTTPStatus.BAD_REQUEST)

        # For the streaming Playback command use the "native" form the Reolink
        # web UI uses: cmd=Playback with channel/type/seek and WITHOUT the
        # output= parameter. cmd=Download (and cmd=Playback with output=) make
        # the camera prepare a temporary file on the SD card, which is
        # unreliable when the card is (nearly) full and can lock up the camera's
        # single VOD session. The plain streaming Playback request does not
        # create a temp file and is reliable. It returns video/x-flv, which is
        # remuxed to fragmented MP4 below so the browser can play it.
        if "cmd=Playback" in reolink_url:
            reolink_url = re.sub(r"&output=[^&]*", "", reolink_url)
            stream_type = (
                1 if stream_res in ("sub", "autotrack_sub", "telephoto_sub") else 0
            )
            if "&channel=" not in reolink_url:
                reolink_url = reolink_url.replace(
                    "cmd=Playback", f"cmd=Playback&channel={ch}", 1
                )
            if "&type=" not in reolink_url:
                reolink_url = f"{reolink_url}&type={stream_type}"
            if "&seek=" not in reolink_url:
                reolink_url = f"{reolink_url}&seek=0"

        headers = dict(request.headers)
        headers.pop("Host", None)
        headers.pop("Referer", None)
        # streaming Playback does not support Range; drop it and answer 200
        headers.pop("Range", None)

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Requested Playback Proxy Method %s, Headers: %s",
                request.method,
                headers,
            )
            _LOGGER.debug(
                "Opening VOD stream from %s: %s",
                host.api.camera_name(ch),
                host.api.hide_password(reolink_url),
            )

        try:
            reolink_response = await self.session.get(
                reolink_url,
                headers=headers,
                timeout=ClientTimeout(
                    connect=15, sock_connect=15, sock_read=5, total=None
                ),
            )
        except ClientError as err:
            err_str = host.api.hide_password(
                f"Reolink playback error while getting video: {err!s}"
            )
            if retry <= 0:
                _LOGGER.warning(err_str)
                return web.Response(body=err_str, status=HTTPStatus.BAD_REQUEST)
            _LOGGER.debug("%s, renewing token", err_str)
            await host.api.expire_session(unsubscribe=False)
            return await self.get(
                request, config_entry_id, channel, stream_res, vod_type, filename, retry
            )

        content_type = reolink_response.content_type

        # Cameras such as the Lumus/Duo return video/x-flv from Playback. The
        # browser <video> element cannot play an FLV container, so remux (stream
        # copy, no re-encode) to fragmented MP4 with ffmpeg and stream that.
        if content_type == "video/x-flv":
            return await self._stream_flv_as_mp4(
                request, reolink_response, clip_seconds
            )

        # Other cameras (e.g. E1 series) return a directly playable mp4.
        if content_type not in (
            "video/mp4",
            "application/octet-stream",
            "apolication/octet-stream",
        ):
            err_str = (
                "Reolink playback expected video/mp4 or video/x-flv"
                f" but got {content_type}"
            )
            _LOGGER.error(err_str)
            if content_type == "text/html":
                _LOGGER.debug(await reolink_response.text())
            reolink_response.close()
            return web.Response(body=err_str, status=HTTPStatus.BAD_REQUEST)

        response_headers = dict(reolink_response.headers)
        _LOGGER.debug(
            "Response Playback Proxy Status %s:%s, Headers: %s",
            reolink_response.status,
            reolink_response.reason,
            response_headers,
        )
        if "Content-Type" not in response_headers:
            response_headers["Content-Type"] = content_type
        if response_headers["Content-Type"] == "apolication/octet-stream":
            response_headers["Content-Type"] = "application/octet-stream"

        response = web.StreamResponse(
            status=reolink_response.status,
            reason=reolink_response.reason,
            headers=response_headers,
        )
        await response.prepare(request)
        try:
            async for chunk in reolink_response.content.iter_chunked(65536):
                await response.write(chunk)
        except TimeoutError:
            _LOGGER.debug(
                "Timeout while reading Reolink playback from %s, writing EOF",
                host.api.nvr_name,
            )
        finally:
            reolink_response.release()
        await response.write_eof()
        return response

    async def _stream_flv_as_mp4(
        self,
        request: web.Request,
        reolink_response,
        clip_seconds: int | None = None,
    ) -> web.StreamResponse:
        """Remux an x-flv playback stream to fragmented MP4 and stream it.

        The camera streams the recording at playback speed, so the data is
        remuxed (stream copy, no re-encode) and forwarded to the browser as it
        arrives; buffering the whole clip first is not viable. ``-t`` tells
        ffmpeg the total length so it can stop at the recording end (instead of
        waiting for the stream to stall) and advertise the duration.
        """
        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "flv",
            "-i",
            "pipe:0",
        ]
        if clip_seconds:
            args += ["-t", str(clip_seconds)]
        args += [
            "-c",
            "copy",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            "pipe:1",
        ]

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        response = web.StreamResponse(
            status=HTTPStatus.OK, headers={"Content-Type": "video/mp4"}
        )
        await response.prepare(request)

        async def _feed() -> None:
            try:
                async for chunk in reolink_response.content.iter_chunked(65536):
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
            except (TimeoutError, ClientError, ConnectionResetError, BrokenPipeError):
                pass
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Reolink remux feeder error", exc_info=True)
            finally:
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass

        feeder = asyncio.ensure_future(_feed())
        try:
            while True:
                data = await proc.stdout.read(65536)
                if not data:
                    break
                await response.write(data)
        except (ConnectionResetError, ClientError):
            pass
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Reolink remux writer error", exc_info=True)
        finally:
            feeder.cancel()
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            reolink_response.close()

        await response.write_eof()
        return response
