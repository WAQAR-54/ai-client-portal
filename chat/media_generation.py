"""Image and video generation via xAI's Grok Imagine API - a genuinely
different capability from chat/providers.py's AIProvider interface (that's
text chat completions; this is two entirely separate REST endpoints with
their own request/response shapes, not something a "messages" call can
express). Deliberately Grok-only, matching the reference "Plan
Capabilities & Limits" mockup's own "Grok only" badge on this feature -
xAI is the only connected provider with an actual media-generation API.

Endpoints and shapes below are taken directly from xAI's own published
docs (docs.x.ai/developers/model-capabilities/images/generation and
.../video/generation) - not guessed. Image generation is synchronous
(the response already contains the final URL); video generation is
submit-then-poll (a request_id, polled until "done"/"failed"/"expired").
"""

import time

import requests

_IMAGE_URL = "https://api.x.ai/v1/images/generations"
_VIDEO_SUBMIT_URL = "https://api.x.ai/v1/videos/generations"
_VIDEO_POLL_URL = "https://api.x.ai/v1/videos/{request_id}"

_IMAGE_MODEL = "grok-imagine-image-2.0"
_VIDEO_MODEL = "grok-imagine-video-1.5"

# Polling budget for video generation - xAI's own docs put typical 720p
# generation at 30-60s. This view call blocks synchronously for that
# whole span (see chat/views.py::generate_media) rather than handing off
# to a background job/polling frontend - a real scaling limit worth
# knowing about before this sees real traffic, but in scope for this pass
# is "make video generation work at all," not "make it non-blocking."
_VIDEO_POLL_INTERVAL_SECONDS = 3
_VIDEO_POLL_MAX_ATTEMPTS = 30  # ~90s


class MediaGenerationError(Exception):
    """Wraps every way image/video generation can fail (no Grok
    connected, upstream HTTP error, generation itself failed/expired,
    timed out waiting) into one type the view catches and turns into a
    user-facing message."""


def _grok_api_key():
    from providers.models import Provider

    # Both raises below are deliberately the same generic, provider-hidden message - same policy
    # chat/views.py::stream_message already follows for a chat reply ("never show the raw upstream
    # error to the user - it can contain the model name or provider identity, which the portal is
    # meant to keep hidden"). This used to say "Grok isn't set up/connected", naming the provider
    # directly to the end user - fixed to match.
    unavailable = MediaGenerationError("Image/video generation isn't available right now. Please try again later.")
    try:
        provider_row = Provider.objects.get(slug="grok")
    except Provider.DoesNotExist as exc:
        raise unavailable from exc
    key = provider_row.get_decrypted_key()
    if not key:
        raise unavailable
    return key


def generate_image(prompt: str) -> bytes:
    """Returns the raw image bytes (PNG/JPEG, whatever xAI actually
    returns) for one generated image. Synchronous - xAI's own response
    already carries the final URL, no polling involved."""
    api_key = _grok_api_key()
    try:
        response = requests.post(
            _IMAGE_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": _IMAGE_MODEL, "prompt": prompt, "n": 1},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        image_url = data["data"][0]["url"]
        image_response = requests.get(image_url, timeout=30)
        image_response.raise_for_status()
        return image_response.content
    except requests.RequestException as exc:
        raise MediaGenerationError("Image generation failed - please try again.") from exc
    except (KeyError, IndexError) as exc:
        # Same generic wording as the request-failure branch above, not a provider-naming message -
        # the cause differs (a malformed/unexpected response shape vs a network error) but there's
        # nothing more actionable to tell the user either way.
        raise MediaGenerationError("Image generation failed - please try again.") from exc


def generate_video(prompt: str) -> bytes:
    """Returns the raw video bytes (mp4) for one generated video. Submits
    the generation request, then polls until xAI reports "done" (and
    downloads the result), "failed"/"expired" (raises), or the polling
    budget above runs out (raises)."""
    api_key = _grok_api_key()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        submit_response = requests.post(
            _VIDEO_SUBMIT_URL,
            headers=headers,
            json={"model": _VIDEO_MODEL, "prompt": prompt},
            timeout=30,
        )
        submit_response.raise_for_status()
        request_id = submit_response.json()["request_id"]

        for _ in range(_VIDEO_POLL_MAX_ATTEMPTS):
            time.sleep(_VIDEO_POLL_INTERVAL_SECONDS)
            poll_response = requests.get(_VIDEO_POLL_URL.format(request_id=request_id), headers=headers, timeout=30)
            poll_response.raise_for_status()
            poll_data = poll_response.json()
            status = poll_data.get("status")
            if status == "done":
                video_url = poll_data["video"]["url"]
                video_response = requests.get(video_url, timeout=60)
                video_response.raise_for_status()
                return video_response.content
            if status in ("failed", "expired"):
                raise MediaGenerationError("Video generation failed - please try again.")
        raise MediaGenerationError("Video generation is taking longer than expected - please try again.")
    except requests.RequestException as exc:
        raise MediaGenerationError("Video generation failed - please try again.") from exc
    except (KeyError, IndexError) as exc:
        # Same reasoning as generate_image's identical branch above.
        raise MediaGenerationError("Video generation failed - please try again.") from exc
