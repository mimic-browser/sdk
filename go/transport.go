// Package mimic manages pinned Mimic runtimes and typed extension calls beside
// native automation clients. Import an adapter subpackage to select a client.
package mimic

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/coder/websocket"
)

// Omitted distinguishes an absent params envelope from explicit JSON null.
var Omitted = omitted{}

type omitted struct{}

type RPCError struct {
	Code    int             `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data,omitempty"`
}

func (e *RPCError) Error() string { return fmt.Sprintf("CDP %d: %s", e.Code, e.Message) }

type Event struct {
	Method    string          `json:"method"`
	Params    json.RawMessage `json:"params,omitempty"`
	SessionID string          `json:"sessionId,omitempty"`
}
type reply struct {
	Result json.RawMessage
	Err    error
}

type pendingCall struct {
	response  chan reply
	sessionID string
}

// Transport owns one CDP socket and routes concurrent calls by request ID.
// Events are delivered in wire order; a slow bounded subscriber is disconnected
// explicitly rather than silently losing browser lifecycle events.
type Transport struct {
	ws          *websocket.Conn
	ctx         context.Context
	cancel      context.CancelFunc
	next        atomic.Int64
	mu          sync.Mutex
	pending     map[int64]pendingCall
	subscribers map[chan Event]struct{}
	err         error
	closed      chan struct{}
}

func Discover(ctx context.Context, endpoint string) (string, error) {
	u, err := url.Parse(endpoint)
	if err != nil || u.Host == "" {
		return "", fmt.Errorf("invalid endpoint: %q", endpoint)
	}
	switch u.Scheme {
	case "ws", "wss":
		return endpoint, nil
	case "http", "https":
	default:
		return "", errors.New("endpoint must use http, https, ws or wss")
	}
	u.Path = strings.TrimRight(u.Path, "/") + "/json/version"
	u.RawQuery = ""
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return "", err
	}
	client := &http.Client{Timeout: 15 * time.Second}
	response, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		return "", fmt.Errorf("CDP discovery: HTTP %d", response.StatusCode)
	}
	var doc struct {
		WebSocketDebuggerURL string `json:"webSocketDebuggerUrl"`
	}
	if err = json.NewDecoder(http.MaxBytesReader(nil, response.Body, 1<<20)).Decode(&doc); err != nil {
		return "", err
	}
	v, e := url.Parse(doc.WebSocketDebuggerURL)
	if e != nil || v.Host == "" || (v.Scheme != "ws" && v.Scheme != "wss") {
		return "", errors.New("discovery lacks a valid browser websocket")
	}
	return doc.WebSocketDebuggerURL, nil
}

func Dial(ctx context.Context, endpoint string) (*Transport, error) {
	endpoint, err := Discover(ctx, endpoint)
	if err != nil {
		return nil, err
	}
	ws, _, err := websocket.Dial(ctx, endpoint, nil)
	if err != nil {
		return nil, err
	}
	ws.SetReadLimit(64 << 20)
	lifetime, cancel := context.WithCancel(context.Background())
	t := &Transport{ws: ws, ctx: lifetime, cancel: cancel, pending: map[int64]pendingCall{}, subscribers: map[chan Event]struct{}{}, closed: make(chan struct{})}
	go t.read()
	return t, nil
}

func (t *Transport) read() {
	defer close(t.closed)
	for {
		_, raw, err := t.ws.Read(t.ctx)
		if err != nil {
			t.fail(err)
			return
		}
		var m struct {
			ID     int64           `json:"id"`
			Result json.RawMessage `json:"result"`
			Error  *RPCError       `json:"error"`
			Event
		}
		if err = json.Unmarshal(raw, &m); err != nil {
			t.fail(fmt.Errorf("invalid CDP envelope: %w", err))
			return
		}
		t.mu.Lock()
		if m.ID != 0 {
			pending, found := t.pending[m.ID]
			delete(t.pending, m.ID)
			if found {
				r := reply{Result: m.Result}
				if m.SessionID != pending.sessionID {
					r.Err = errors.New("CDP response session does not match the request")
				} else if m.Error != nil {
					r.Err = m.Error
				}
				pending.response <- r
			}
		} else if m.Method != "" {
			for ch := range t.subscribers {
				select {
				case ch <- m.Event:
				default:
					t.mu.Unlock()
					t.fail(errors.New("CDP event subscriber overflow"))
					return
				}
			}
		}
		t.mu.Unlock()
	}
}
func (t *Transport) fail(err error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.err != nil {
		return
	}
	t.err = err
	t.cancel()
	_ = t.ws.CloseNow()
	for id, pending := range t.pending {
		pending.response <- reply{Err: err}
		delete(t.pending, id)
	}
	for ch := range t.subscribers {
		close(ch)
		delete(t.subscribers, ch)
	}
}
func (t *Transport) Close() error { t.fail(errors.New("CDP transport closed")); <-t.closed; return nil }
func (t *Transport) Subscribe() (events <-chan Event, cancel func()) {
	ch := make(chan Event, 1024)
	t.mu.Lock()
	if t.err != nil {
		close(ch)
	} else {
		t.subscribers[ch] = struct{}{}
	}
	t.mu.Unlock()
	return ch, func() {
		t.mu.Lock()
		defer t.mu.Unlock()
		if _, ok := t.subscribers[ch]; ok {
			delete(t.subscribers, ch)
			close(ch)
		}
	}
}

// CallRaw validates only the envelope/JSON representation. It never discovers
// command membership, retries commands, or assumes cancellation rolls back RPC.
func (t *Transport) CallRaw(ctx context.Context, method string, params any, sessionID string) (json.RawMessage, error) {
	if !strings.Contains(method, ".") || strings.TrimSpace(method) != method {
		return nil, errors.New("expected a qualified CDP method")
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	id := t.next.Add(1)
	m := map[string]any{"id": id, "method": method}
	if _, omit := params.(omitted); !omit {
		m["params"] = params
	}
	if sessionID != "" {
		m["sessionId"] = sessionID
	}
	raw, err := json.Marshal(m)
	if err != nil {
		return nil, err
	}
	ch := make(chan reply, 1)
	t.mu.Lock()
	if t.err != nil {
		err = t.err
		t.mu.Unlock()
		return nil, err
	}
	t.pending[id] = pendingCall{response: ch, sessionID: sessionID}
	t.mu.Unlock()
	defer func() { t.mu.Lock(); delete(t.pending, id); t.mu.Unlock() }()
	if err = t.ws.Write(ctx, websocket.MessageText, raw); err != nil {
		return nil, err
	}
	select {
	case r := <-ch:
		return r.Result, r.Err
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

type RawCaller interface {
	CallRaw(context.Context, string, any, string) (json.RawMessage, error)
}
type Experimental struct {
	transport RawCaller
	sessionID string
}

func (e *Experimental) Call(ctx context.Context, name string, params any) (json.RawMessage, error) {
	if name == "" || strings.ContainsAny(name, ". \t\n\r") {
		return nil, errors.New("experimental command requires an exact unqualified wire leaf")
	}
	return e.transport.CallRaw(ctx, "Mimic."+name, params, e.sessionID)
}

type Client struct {
	MimicCommands
	transport    RawCaller
	sessionID    string
	Experimental *Experimental
}

func NewClient(transport RawCaller, sessionID string) *Client {
	c := &Client{transport: transport, sessionID: sessionID, Experimental: &Experimental{transport: transport, sessionID: sessionID}}
	c.MimicCommands.Sender = c
	return c
}
func (c *Client) Call(ctx context.Context, method string, params any, result any) error {
	raw, err := c.transport.CallRaw(ctx, method, params, c.sessionID)
	if err != nil {
		return err
	}
	if result == nil {
		return nil
	}
	return json.Unmarshal(raw, result)
}
