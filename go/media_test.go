package mimic_test

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"

	cdpruntime "github.com/chromedp/cdproto/runtime"
	native "github.com/chromedp/chromedp"
	"github.com/go-rod/rod/lib/proto"
	mimic "github.com/mimic-browser/sdk/go"
	mimicchromedp "github.com/mimic-browser/sdk/go/chromedp"
	mimicrod "github.com/mimic-browser/sdk/go/rod"
)

func TestNativeMediaFactories(t *testing.T) {
	binary := os.Getenv("MIMIC_MEDIA_FIXTURE")
	if runtime.GOOS != "linux" || binary == "" {
		t.Skip("requires explicit synthetic-only MIMIC_MEDIA_FIXTURE in Linux/WSL")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, binary, "--browser-mode", "headless", "--listen", "127.0.0.1:0")
	command.Stderr = os.Stderr
	stdout, err := command.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err = command.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = command.Process.Kill(); _ = command.Wait() }()
	lines := bufio.NewScanner(stdout)
	var endpoint, fixture string
	for (endpoint == "" || fixture == "") && lines.Scan() {
		if strings.HasPrefix(lines.Text(), "Mimic listening on ") {
			endpoint = strings.TrimPrefix(lines.Text(), "Mimic listening on ")
		}
		if strings.HasPrefix(lines.Text(), "Fixture listening on ") {
			fixture = strings.TrimPrefix(lines.Text(), "Fixture listening on ")
		}
	}
	if endpoint == "" || fixture == "" {
		t.Fatalf("synthetic fixture did not become ready: %v", lines.Err())
	}
	transport, err := mimic.Dial(ctx, endpoint)
	if err != nil {
		t.Fatal(err)
	}
	defer transport.Close()
	control := mimic.NewClient(transport, "")
	contextIDs := func(t *testing.T) json.RawMessage {
		t.Helper()
		var contexts json.RawMessage
		if err := control.Call(ctx, "Target.getBrowserContexts", mimic.Omitted, &contexts); err != nil {
			t.Fatal(err)
		}
		return contexts
	}
	for _, framework := range []string{"rod", "chromedp"} {
		t.Run(framework, func(t *testing.T) {
			var sourceID, contextID string
			factory := func(ctx context.Context, setup mimic.ContextSetup) (mimic.MediaConfiguration, error) {
				contextID = setup.BrowserContextID
				sources, err := setup.Mimic.GetMediaSources(ctx, mimic.GetMediaSourcesParams{BrowserContextId: mimic.Some(contextID)})
				if err != nil {
					return mimic.MediaConfiguration{}, err
				}
				source := func(label string) json.RawMessage {
					for _, value := range sources.Sources {
						if value.Label == label {
							if strings.Contains(value.SourceId, "native") {
								t.Fatalf("source ID exposes backend identity: %s", value.SourceId)
							}
							if value.Kind == "videoinput" {
								sourceID = value.SourceId
							}
							wire, _ := json.Marshal(map[string]string{"sourceId": value.SourceId})
							return wire
						}
					}
					t.Fatalf("synthetic source not found: %s", label)
					return nil
				}
				mode := mimic.CameraFormat{Width: 16, Height: 8, FrameRate: 30}
				return mimic.MediaConfiguration{Devices: mimic.Some([]mimic.MediaDeviceProfile{
					{Key: "front", Kind: "videoinput", Label: "Studio Camera", Group: mimic.Some("desk"), Source: source("Private native camera B"), Modes: mimic.Some([]mimic.CameraFormat{mode}), DefaultMode: mimic.Some(mode), Processing: mimic.Some(mimic.MediaProcessing{Resize: mimic.Some("crop-and-scale")})},
					{Key: "voice", Kind: "audioinput", Label: "Studio Microphone", Group: mimic.Some("desk"), Source: source("Private native microphone B")},
				})}, nil
			}
			grant := func() {
				t.Helper()
				if err := control.Call(ctx, "Browser.grantPermissions", map[string]any{"browserContextId": contextID, "origin": fixture, "permissions": []string{"videoCapture", "audioCapture"}}, nil); err != nil {
					t.Fatal(err)
				}
			}
			failure := errors.New("factory rejected configuration")
			failing := func(context.Context, mimic.ContextSetup) (mimic.MediaConfiguration, error) {
				return mimic.MediaConfiguration{}, failure
			}
			var observed struct {
				Devices []struct{ Label, DeviceID, GroupID string }
				Pixel   []int
				Tracks  []struct {
					Label    string
					Settings struct{ DeviceID, GroupID string }
				}
			}
			expression := `(async () => {
              const devices = await navigator.mediaDevices.enumerateDevices();
              const camera = devices.find(item => item.label === 'Studio Camera');
              const microphone = devices.find(item => item.label === 'Studio Microphone');
              const stream = await navigator.mediaDevices.getUserMedia({
                video: {deviceId: {exact: camera.deviceId}},
                audio: {deviceId: {exact: microphone.deviceId}}
              });
              const video = document.createElement('video');
              video.srcObject = stream;
              document.body.append(video);
              await video.play();
              await new Promise(resolve => video.requestVideoFrameCallback(resolve));
              const canvas = document.createElement('canvas');
              canvas.width = 16;
              canvas.height = 8;
              const draw = canvas.getContext('2d');
              draw.drawImage(video, 0, 0, 16, 8);
              const result = {
                devices: devices.map(item => item.toJSON()),
                pixel: Array.from(draw.getImageData(0, 0, 1, 1).data),
                tracks: stream.getTracks().map(track => ({label: track.label, settings: track.getSettings()}))
              };
              stream.getTracks().forEach(track => track.stop());
              return result;
            })()`
			if framework == "rod" {
				session, err := mimicrod.Connect(ctx, endpoint)
				if err != nil {
					t.Fatal(err)
				}
				defer session.Close()
				before := contextIDs(t)
				if _, err := session.NewConfiguredContextWithMedia(ctx, mimic.ConfigureContextParams{}, failing); !errors.Is(err, failure) {
					t.Fatalf("expected factory error: %v", err)
				}
				if string(contextIDs(t)) != string(before) {
					t.Fatal("failed factory retained its Context")
				}
				closing, err := mimicrod.Connect(ctx, endpoint)
				if err != nil {
					t.Fatal(err)
				}
				defer closing.Close()
				created, err := closing.NewConfiguredContextWithMedia(ctx, mimic.ConfigureContextParams{}, func(context.Context, mimic.ContextSetup) (mimic.MediaConfiguration, error) {
					return mimic.MediaConfiguration{}, closing.Close()
				})
				if err == nil || created != nil {
					t.Fatalf("factory closing its Session must fail without returning a Context: %v %v", created, err)
				}
				if string(contextIDs(t)) != string(before) {
					t.Fatal("factory closing its Session retained an owned Context")
				}
				browser, err := session.NewConfiguredContextWithMedia(ctx, mimic.ConfigureContextParams{}, func(ctx context.Context, setup mimic.ContextSetup) (mimic.MediaConfiguration, error) {
					// Reentrant native Context creation proves the callback runs
					// outside the adapter's ownership mutex.
					if _, err := session.NewContext(ctx, nil, nil); err != nil {
						return mimic.MediaConfiguration{}, err
					}
					return factory(ctx, setup)
				})
				if err != nil {
					t.Fatal(err)
				}
				grant()
				page, err := browser.Page(proto.TargetCreateTarget{URL: fixture})
				if err != nil {
					t.Fatal(err)
				}
				value, err := page.Eval("() => " + expression)
				if err != nil {
					t.Fatal(err)
				}
				if err := value.Value.Unmarshal(&observed); err != nil {
					t.Fatal(err)
				}
			} else {
				before := contextIDs(t)
				if _, err := mimicchromedp.ConnectConfiguredWithMedia(ctx, endpoint, mimic.ConfigureContextParams{}, failing); !errors.Is(err, failure) {
					t.Fatalf("expected factory error: %v", err)
				}
				if string(contextIDs(t)) != string(before) {
					t.Fatal("failed factory retained its Context")
				}
				session, err := mimicchromedp.ConnectConfiguredWithMedia(ctx, endpoint, mimic.ConfigureContextParams{}, factory)
				if err != nil {
					t.Fatal(err)
				}
				defer session.Close()
				grant()
				if err := native.Run(session.Context, native.Navigate(fixture), native.Evaluate(expression, &observed, func(params *cdpruntime.EvaluateParams) *cdpruntime.EvaluateParams {
					return params.WithAwaitPromise(true)
				})); err != nil {
					t.Fatal(err)
				}
			}
			if !reflect.DeepEqual(observed.Pixel, []int{0, 0, 255, 255}) {
				t.Fatalf("logical camera did not capture source B: %#v", observed.Pixel)
			}
			if len(observed.Devices) != 2 || len(observed.Tracks) != 2 {
				t.Fatalf("unexpected public media devices: %#v", observed)
			}
			if observed.Devices[0].GroupID != observed.Devices[1].GroupID || observed.Devices[0].DeviceID == observed.Devices[1].DeviceID {
				t.Fatal("public device/group identity mismatch")
			}
			wire, _ := json.Marshal(observed)
			if strings.Contains(string(wire), "Private native") || strings.Contains(string(wire), sourceID) {
				t.Fatal("web media observation exposed private capture identity")
			}
			for _, track := range observed.Tracks {
				found := false
				for _, device := range observed.Devices {
					if device.Label == track.Label && track.Settings.DeviceID == device.DeviceID && track.Settings.GroupID == device.GroupID {
						found = true
					}
				}
				if !found {
					t.Fatalf("track identity does not match public device: %#v", track)
				}
			}
		})
	}
}
