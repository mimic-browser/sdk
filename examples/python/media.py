"""Select private capture sources and expose independent public device identities."""
import os

from mimic.generated import GetMediaSourcesParams, MediaConfiguration, MediaDeviceProfile
from mimic.playwright.sync_api import ContextSetup, launch


def media(setup: ContextSetup) -> MediaConfiguration:
    sources = setup.mimic.get_media_sources(
        GetMediaSourcesParams(browser_context_id=setup.browser_context_id)
    ).sources
    camera = next(source for source in sources if source.label == "OBS Virtual Camera")
    microphone = next(source for source in sources if source.kind == "audioinput" and source.default)
    return MediaConfiguration(seed="meeting-devices", devices=[
        MediaDeviceProfile(key="camera", kind="videoinput", source={"sourceId": camera.source_id},
                           label="Studio Camera", group="desk",
                           modes=[{"width": 640, "height": 480, "frameRate": 30}],
                           default_mode={"width": 640, "height": 480, "frameRate": 30},
                           processing={"resize": "crop-and-scale"}),
        MediaDeviceProfile(key="microphone", kind="audioinput", source={"sourceId": microphone.source_id},
                           label="Studio Microphone", group="desk"),
    ])


# The current development runtime contains the managed-context bridge.
with launch(executable_path=os.environ["MIMIC_EXECUTABLE_PATH"]) as session:
    context = session.new_context(profile={"generate": {"seed": "desktop-persona"}}, media=media)
    # Configuration alone does not grant permission or open capture.
    context.grant_permissions(["camera", "microphone"], origin="https://example.com")
    page = context.new_page()
    page.goto("https://example.com")
    print(page.evaluate("async () => (await navigator.mediaDevices.enumerateDevices()).map(device => device.toJSON())"))
