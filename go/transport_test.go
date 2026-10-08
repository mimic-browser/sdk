package mimic

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func TestRawRoutingErrorsOmissionAndCancellation(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("listener tests run in WSL to avoid firewall prompts")
	}
	var mu sync.Mutex
	var seen []map[string]json.RawMessage
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer conn.CloseNow()
		for {
			_, b, err := conn.Read(r.Context())
			if err != nil {
				return
			}
			var m map[string]json.RawMessage
			if json.Unmarshal(b, &m) != nil {
				return
			}
			mu.Lock()
			seen = append(seen, m)
			mu.Unlock()
			var method string
			_ = json.Unmarshal(m["method"], &method)
			if method == "Mimic.timeout" {
				continue
			}
			response := map[string]any{"id": m["id"], "result": map[string]any{"params": m["params"], "session": m["sessionId"]}}
			if method == "Mimic.unknown" {
				response = map[string]any{"id": m["id"], "error": map[string]any{"code": -32601, "message": "not found", "data": map[string]any{"exact": true}}}
			}
			response["sessionId"] = m["sessionId"]
			if method == "Mimic.wrongSession" {
				response["sessionId"] = "another-page"
			}
			b, _ = json.Marshal(response)
			if conn.Write(r.Context(), websocket.MessageText, b) != nil {
				return
			}
		}
	}))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	tx, err := Dial(ctx, "ws"+strings.TrimPrefix(server.URL, "http"))
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Close()
	c := NewClient(tx, "page-session")
	for _, value := range []any{Omitted, nil, map[string]any{"nested": []any{nil, true, "λ"}}} {
		if _, err = c.Experimental.Call(ctx, "newUnschematizedFeature", value); err != nil {
			t.Fatal(err)
		}
	}
	_, err = c.Experimental.Call(ctx, "unknown", map[string]any{})
	var protocol *RPCError
	if !errors.As(err, &protocol) || protocol.Code != -32601 || string(protocol.Data) != `{"exact":true}` {
		t.Fatalf("error envelope lost: %#v", err)
	}
	timeout, stop := context.WithTimeout(ctx, 10*time.Millisecond)
	defer stop()
	if _, err = c.Experimental.Call(timeout, "timeout", Omitted); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for i := 0; i < 30; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			raw, e := c.Experimental.Call(ctx, "newUnschematizedFeature", map[string]any{"i": i})
			if e != nil {
				t.Error(e)
				return
			}
			var result struct {
				Params struct {
					I int `json:"i"`
				} `json:"params"`
				Session string `json:"session"`
			}
			if json.Unmarshal(raw, &result) != nil || result.Params.I != i || result.Session != "page-session" {
				t.Errorf("routing mismatch: %s", raw)
			}
		}(i)
	}
	wg.Wait()
	if _, err = c.Experimental.Call(ctx, "wrongSession", Omitted); err == nil || !strings.Contains(err.Error(), "session") {
		t.Fatalf("cross-session reply accepted: %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if _, ok := seen[0]["params"]; ok {
		t.Fatal("omitted params became present")
	}
	if string(seen[1]["params"]) != "null" {
		t.Fatal("explicit null lost")
	}
	if len(seen) != 36 {
		t.Fatalf("unexpected discovery/retry calls: %d", len(seen))
	}
}
