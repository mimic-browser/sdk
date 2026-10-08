package rod

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"runtime"
	"testing"
	"time"

	"github.com/go-rod/rod/lib/proto"
	mimic "github.com/mimic-browser/sdk/go"
)

func TestRealRodOwnedAndAttached(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Listener integration runs in WSL")
	}
	bin := os.Getenv("MIMIC_SDK_TEST_RUNTIME")
	if bin == "" {
		t.Skip("set MIMIC_SDK_TEST_RUNTIME inside WSL")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	s, err := Launch(ctx, mimic.RuntimeOptions{ExecutablePath: bin})
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	attached, err := Connect(ctx, s.Runtime.Endpoint)
	if err != nil {
		t.Fatal(err)
	}
	b, err := attached.NewContext(ctx, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte("<h1>Native Rod</h1>")) }))
	defer fixture.Close()
	p, err := b.Page(proto.TargetCreateTarget{URL: fixture.URL})
	if err != nil {
		t.Fatal(err)
	}
	value, err := p.Eval(`() => document.querySelector('h1').textContent`)
	if err != nil {
		t.Fatal(err)
	}
	if value.Value.Str() != "Native Rod" {
		t.Fatal(value.Value)
	}
	if _, err = attached.ForPage(p).Experimental.Call(ctx, "getTrace", map[string]any{}); err != nil {
		t.Fatal(err)
	}
	pageMimic := attached.ForPage(p)
	if pageMimic != attached.ForPage(p) {
		t.Fatal("native Page handle not cached")
	}
	if err := attached.DetachPage(ctx, p); err != nil {
		t.Fatal(err)
	}
	if _, err := pageMimic.Experimental.Call(ctx, "getTrace", map[string]any{}); err == nil {
		t.Fatal("detached handle still usable")
	}
	if value, err := p.Eval(`() => 1+2`); err != nil || value.Value.Int() != 3 {
		t.Fatalf("SDK detach affected native Rod session: %v %v", value, err)
	}
	pageMimic = attached.ForPage(p)
	if err = attached.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := pageMimic.Experimental.Call(ctx, "getTrace", map[string]any{}); err == nil {
		t.Fatal("closed owner retained page extension")
	}
	if _, err = s.Mimic.GetVersion(ctx, mimic.GetVersionParams{}); err != nil {
		t.Fatalf("attached close killed owner: %v", err)
	}
	if os.Getenv("MIMIC_SDK_TEST_CONFIGURE") == "1" {
		configured, err := s.NewConfiguredContext(ctx, mimic.ConfigureContextParams{Profile: mimic.Some(json.RawMessage(`{"generate":{"seed":"go-rod"}}`))})
		if err != nil {
			t.Fatal(err)
		}
		page, err := configured.Page(proto.TargetCreateTarget{URL: "about:blank"})
		if err != nil {
			t.Fatal(err)
		}
		value, err := page.Eval(`() => navigator.hardwareConcurrency`)
		if err != nil || value.Value.Int() <= 0 {
			t.Fatalf("managed native page: %v %v", value, err)
		}
	}
}
