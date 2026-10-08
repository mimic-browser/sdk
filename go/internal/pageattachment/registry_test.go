package pageattachment

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	mimic "github.com/mimic-browser/sdk/go"
)

type fakeWire struct {
	mu                           sync.Mutex
	attached, detached, commands int
	entered                      chan struct{}
	release                      chan struct{}
	detachEntered                chan struct{}
	detachRelease                chan struct{}
	detachError                  error
}

func (w *fakeWire) CallRaw(ctx context.Context, method string, params any, sid string) (json.RawMessage, error) {
	w.mu.Lock()
	switch method {
	case "Target.attachToTarget":
		w.attached++
		id := w.attached
		w.mu.Unlock()
		if w.entered != nil {
			w.entered <- struct{}{}
			select {
			case <-w.release:
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		return json.RawMessage(fmt.Sprintf(`{"sessionId":"owned-%d"}`, id)), nil
	case "Target.detachFromTarget":
		w.detached++
		w.mu.Unlock()
		if w.detachEntered != nil {
			w.detachEntered <- struct{}{}
			select {
			case <-w.detachRelease:
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		return json.RawMessage(`{}`), w.detachError
	default:
		w.commands++
	}
	w.mu.Unlock()
	return json.RawMessage(`{}`), nil
}

func registryFixture(t *testing.T, wire *fakeWire) *Registry {
	t.Helper()
	events := make(chan mimic.Event, 8)
	var closeEvents sync.Once
	r := newRegistry(wire, events, func() { closeEvents.Do(func() { close(events) }) })
	t.Cleanup(func() { _ = r.Close(context.Background()) })
	return r
}

func TestConcurrentAcquisitionCancellationAndExplicitDetach(t *testing.T) {
	wire := &fakeWire{entered: make(chan struct{}, 1), release: make(chan struct{})}
	r := registryFixture(t, wire)
	ctx, cancel := context.WithCancel(context.Background())
	first := make(chan error, 1)
	go func() { _, err := r.Attach(ctx, "page"); first <- err }()
	<-wire.entered
	cancel()
	if err := <-first; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	const count = 8
	clients := make(chan *mimic.Client, count)
	var callers sync.WaitGroup
	for range count {
		callers.Add(1)
		go func() {
			defer callers.Done()
			client, err := r.Attach(context.Background(), "page")
			if err != nil {
				t.Error(err)
			}
			clients <- client
		}()
	}
	close(wire.release)
	callers.Wait()
	close(clients)
	var firstClient *mimic.Client
	for client := range clients {
		if firstClient == nil {
			firstClient = client
		}
		if client != firstClient {
			t.Fatal("same native Page acquired multiple extension Clients")
		}
	}
	if firstClient == nil {
		t.Fatal("missing page client")
	}
	if err := r.Detach(context.Background(), "page"); err != nil {
		t.Fatal(err)
	}
	if err := firstClient.Call(context.Background(), "Mimic.getTrace", map[string]any{}, nil); !errors.Is(err, errClosed) {
		t.Fatalf("stale typed handle: %v", err)
	}
	if _, err := firstClient.Experimental.Call(context.Background(), "getTrace", map[string]any{}); !errors.Is(err, errClosed) {
		t.Fatalf("stale experimental handle: %v", err)
	}
	wire.mu.Lock()
	defer wire.mu.Unlock()
	if wire.attached != 1 || wire.detached != 1 || wire.commands != 0 {
		t.Fatalf("wire calls: attach=%d detach=%d commands=%d", wire.attached, wire.detached, wire.commands)
	}
}

func TestBorrowedSessionDetachPreservesNativeOwnership(t *testing.T) {
	wire := &fakeWire{}
	r := registryFixture(t, wire)
	client := r.Borrow("page", "native")
	if r.Borrow("page", "native") != client {
		t.Fatal("borrowed handle not cached")
	}
	if err := r.Detach(context.Background(), "page"); err != nil {
		t.Fatal(err)
	}
	if err := client.Call(context.Background(), "Mimic.getTrace", nil, nil); !errors.Is(err, errClosed) {
		t.Fatal(err)
	}
	second := r.Borrow("page", "native")
	if second == client {
		t.Fatal("detached handle reused")
	}
	if err := second.Call(context.Background(), "Mimic.getTrace", nil, nil); err != nil {
		t.Fatal(err)
	}
	if err := r.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := second.Call(context.Background(), "Mimic.getTrace", nil, nil); !errors.Is(err, errClosed) {
		t.Fatal(err)
	}
	if _, err := r.Attach(context.Background(), "other"); !errors.Is(err, errClosed) {
		t.Fatal(err)
	}
	wire.mu.Lock()
	defer wire.mu.Unlock()
	if wire.attached != 0 || wire.detached != 0 {
		t.Fatal("SDK detached a native-owned session")
	}
}

func TestDetachEventInvalidatesPendingAndPublishedHandles(t *testing.T) {
	for _, pending := range []bool{false, true} {
		t.Run(fmt.Sprint(pending), func(t *testing.T) {
			wire := &fakeWire{entered: make(chan struct{}, 1), release: make(chan struct{})}
			r := registryFixture(t, wire)
			done := make(chan error, 1)
			var client *mimic.Client
			go func() { var err error; client, err = r.Attach(context.Background(), "page"); done <- err }()
			<-wire.entered
			if !pending {
				close(wire.release)
				if err := <-done; err != nil {
					t.Fatal(err)
				}
			}
			r.invalidate(mimic.Event{Method: "Target.detachedFromTarget", Params: json.RawMessage(`{"sessionId":"owned-1","targetId":"page"}`)})
			if pending {
				close(wire.release)
				if err := <-done; !errors.Is(err, errClosed) {
					t.Fatal(err)
				}
			} else {
				if err := client.Call(context.Background(), "Mimic.getTrace", nil, nil); !errors.Is(err, errClosed) {
					t.Fatal(err)
				}
			}
			r.mu.Lock()
			remaining := len(r.pages)
			r.mu.Unlock()
			if remaining != 0 {
				t.Fatal("closed target retained in cache")
			}
		})
	}
}

func TestCloseJoinsPendingAcquisition(t *testing.T) {
	wire := &fakeWire{entered: make(chan struct{}, 1), release: make(chan struct{})}
	r := registryFixture(t, wire)
	done := make(chan error, 1)
	go func() { _, err := r.Attach(context.Background(), "page"); done <- err }()
	<-wire.entered
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := r.Close(ctx); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err == nil {
		t.Fatal("acquisition succeeded after owner close")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.pages) != 0 {
		t.Fatal("pending attachment retained after close")
	}
}

func TestCloseJoinsExplicitDetachAfterRemoteEvent(t *testing.T) {
	wire := &fakeWire{detachEntered: make(chan struct{}, 1), detachRelease: make(chan struct{})}
	r := registryFixture(t, wire)
	if _, err := r.Attach(context.Background(), "page"); err != nil {
		t.Fatal(err)
	}
	detached := make(chan error, 1)
	go func() { detached <- r.Detach(context.Background(), "page") }()
	<-wire.detachEntered
	r.invalidate(mimic.Event{Method: "Target.detachedFromTarget", Params: json.RawMessage(`{"sessionId":"owned-1","targetId":"page"}`)})
	closed := make(chan error, 1)
	go func() { closed <- r.Close(context.Background()) }()
	<-r.lifetime.Done()
	select {
	case err := <-closed:
		t.Fatalf("owner closed before detach reply: %v", err)
	default:
	}
	close(wire.detachRelease)
	if err := <-detached; err != nil {
		t.Fatal(err)
	}
	if err := <-closed; err != nil {
		t.Fatal(err)
	}
}

func TestLateCleanupFailureRemainsVisibleToOwner(t *testing.T) {
	protocol := &mimic.RPCError{Code: -32000, Message: "cleanup failed", Data: json.RawMessage(`{"reason":"fixture"}`)}
	wire := &fakeWire{entered: make(chan struct{}, 1), release: make(chan struct{}), detachError: protocol}
	r := registryFixture(t, wire)
	done := make(chan error, 1)
	go func() { _, err := r.Attach(context.Background(), "page"); done <- err }()
	<-wire.entered
	r.invalidate(mimic.Event{Method: "Target.detachedFromTarget", Params: json.RawMessage(`{"sessionId":"owned-1"}`)})
	close(wire.release)
	if err := <-done; !errors.Is(err, protocol) {
		t.Fatalf("attachment lost cleanup error: %v", err)
	}
	if err := r.Close(context.Background()); !errors.Is(err, protocol) {
		t.Fatalf("owner lost cleanup error: %v", err)
	}
}
