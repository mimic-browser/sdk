package chromedp

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"runtime"
	"testing"
	"time"

	native "github.com/chromedp/chromedp"
	mimic "github.com/mimic-browser/sdk/go"
)

func TestRealChromedpOwnedAndAttached(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Listener integration runs in WSL")
	}
	bin := os.Getenv("MIMIC_SDK_TEST_RUNTIME")
	if bin == "" {
		t.Skip("set MIMIC_SDK_TEST_RUNTIME inside WSL")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	var options []native.ContextOption
	if os.Getenv("MIMIC_SDK_DEBUG_PROTOCOL") == "1" {
		options = append(options, native.WithDebugf(t.Logf))
	}
	s, err := Launch(ctx, mimic.RuntimeOptions{ExecutablePath: bin}, options...)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	var text string
	fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte("<h1>Native chromedp</h1>")) }))
	defer fixture.Close()
	if err = native.Run(s.Context, native.Navigate(fixture.URL), native.Text("h1", &text, native.ByQuery)); err != nil {
		t.Fatal(err)
	}
	if text != "Native chromedp" {
		t.Fatal(text)
	}
	attached, err := Connect(ctx, s.Runtime.Endpoint)
	if err != nil {
		t.Fatal(err)
	}
	if err = attached.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err = s.Mimic.GetVersion(ctx, mimic.GetVersionParams{}); err != nil {
		t.Fatalf("attached close killed owner: %v", err)
	}
	pageMimic, err := s.ForPage(s.Context)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = pageMimic.Experimental.Call(ctx, "getTrace", map[string]any{}); err != nil {
		t.Fatal(err)
	}
	if os.Getenv("MIMIC_SDK_TEST_CONFIGURE") == "1" {
		configured, err := ConnectConfigured(ctx, s.Runtime.Endpoint, mimic.ConfigureContextParams{Profile: mimic.Some(json.RawMessage(`{"generate":{"seed":"go-chromedp"}}`))})
		if err != nil {
			t.Fatal(err)
		}
		defer configured.Close()
		var processors int
		if err = native.Run(configured.Context, native.Evaluate(`navigator.hardwareConcurrency`, &processors)); err != nil || processors <= 0 {
			t.Fatalf("managed native target: %d %v", processors, err)
		}
	}
}
