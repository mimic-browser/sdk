// Package pageattachment owns extension handles without changing native Pages.
package pageattachment

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	mimic "github.com/mimic-browser/sdk/go"
)

var errClosed = errors.New("page extension attachment closed")

type attachment struct {
	transport mimic.RawCaller
	id        string
	owned     bool
	active    atomic.Bool
	client    *mimic.Client
	ready     chan struct{}
	err       error
	detached  map[string]struct{}
	released  atomic.Bool
}

func (p *attachment) CallRaw(ctx context.Context, method string, params any, sessionID string) (json.RawMessage, error) {
	if !p.active.Load() {
		return nil, errClosed
	}
	return p.transport.CallRaw(ctx, method, params, sessionID)
}

// Registry deduplicates SDK attachments and invalidates already-returned handles.
// Borrowed native sessions are never detached by this registry.
type Registry struct {
	transport   mimic.RawCaller
	mu          sync.Mutex
	pages       map[string]*attachment
	closed      bool
	unsubscribe func()
	done        chan struct{}
	lifetime    context.Context
	cancel      context.CancelFunc
	workers     sync.WaitGroup
	closing     bool
	closeDone   chan struct{}
	closeErr    error
	cleanupErr  error
}

func New(transport *mimic.Transport) *Registry {
	events, unsubscribe := transport.Subscribe()
	return newRegistry(transport, events, unsubscribe)
}

func newRegistry(transport mimic.RawCaller, events <-chan mimic.Event, unsubscribe func()) *Registry {
	r := &Registry{transport: transport, pages: make(map[string]*attachment), unsubscribe: unsubscribe, done: make(chan struct{})}
	r.lifetime, r.cancel = context.WithCancel(context.Background())
	r.closeDone = make(chan struct{})
	go func() {
		defer close(r.done)
		for event := range events {
			r.invalidate(event)
		}
		r.mu.Lock()
		r.closed = true
		for _, p := range r.pages {
			p.active.Store(false)
		}
		clear(r.pages)
		r.mu.Unlock()
	}()
	return r
}

func (r *Registry) invalidate(event mimic.Event) {
	if event.Method != "Target.detachedFromTarget" && event.Method != "Target.targetDestroyed" && event.Method != "Inspector.detached" {
		return
	}
	var params struct {
		SessionID string `json:"sessionId"`
		TargetID  string `json:"targetId"`
	}
	if json.Unmarshal(event.Params, &params) != nil {
		return
	}
	if event.Method == "Inspector.detached" {
		params.SessionID = event.SessionID
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	for target, p := range r.pages {
		if p.id == "" && p.owned && params.SessionID != "" {
			p.detached[params.SessionID] = struct{}{}
		}
		if (params.SessionID != "" && p.id == params.SessionID) ||
			(params.TargetID == target && event.Method == "Target.targetDestroyed") {
			p.active.Store(false)
			p.released.Store(true)
			delete(r.pages, target)
		}
	}
}

// Borrow binds an existing native session on the same transport. Invalid input
// or a closed owner produces a handle whose commands fail explicitly.
func (r *Registry) Borrow(target, sessionID string) *mimic.Client {
	r.mu.Lock()
	defer r.mu.Unlock()
	if p := r.pages[target]; p != nil && p.id == sessionID && p.active.Load() {
		return p.client
	}
	p := &attachment{transport: r.transport, id: sessionID}
	p.client = mimic.NewClient(p, sessionID)
	if r.closed || target == "" || sessionID == "" {
		return p.client
	}
	if old := r.pages[target]; old != nil {
		old.active.Store(false)
	}
	p.active.Store(true)
	r.pages[target] = p
	return p.client
}

func (r *Registry) Attach(ctx context.Context, target string) (*mimic.Client, error) {
	if target == "" {
		return nil, errors.New("native page target is required")
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	r.mu.Lock()
	if r.closed {
		r.mu.Unlock()
		return nil, errClosed
	}
	p := r.pages[target]
	if p == nil {
		p = &attachment{transport: r.transport, owned: true, ready: make(chan struct{}), detached: make(map[string]struct{})}
		p.active.Store(true)
		r.pages[target] = p
		r.workers.Add(1)
		go r.acquire(target, p)
	}
	r.mu.Unlock()
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-p.ready:
		if p.err != nil {
			return nil, p.err
		}
		if !p.active.Load() {
			return nil, errClosed
		}
		return p.client, nil
	}
}

func (r *Registry) acquire(target string, p *attachment) {
	defer r.workers.Done()
	defer close(p.ready)
	// Caller cancellation stops that wait, not the owned acquisition. Its reply
	// must still be consumed so a successful remote attachment can be released.
	ctx, cancel := context.WithTimeout(r.lifetime, 30*time.Second)
	defer cancel()
	raw, err := r.transport.CallRaw(ctx, "Target.attachToTarget", map[string]any{"targetId": target, "flatten": true}, "")
	var result struct {
		SessionID string `json:"sessionId"`
	}
	if err == nil {
		err = json.Unmarshal(raw, &result)
	}
	if err == nil && result.SessionID == "" {
		err = errors.New("Target.attachToTarget omitted sessionId")
	}
	r.mu.Lock()
	if err != nil {
		p.err = err
		p.active.Store(false)
		if r.pages[target] == p {
			delete(r.pages, target)
		}
		r.mu.Unlock()
		return
	}
	_, detached := p.detached[result.SessionID]
	p.detached = nil
	p.id = result.SessionID
	if r.closed || !p.active.Load() || r.pages[target] != p || detached {
		p.err = errClosed
		p.active.Store(false)
		if r.pages[target] == p {
			delete(r.pages, target)
		}
		r.mu.Unlock()
		cleanup, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if cleanupErr := r.detach(cleanup, result.SessionID); cleanupErr != nil {
			r.mu.Lock()
			r.cleanupErr = errors.Join(r.cleanupErr, cleanupErr)
			p.err = errors.Join(p.err, cleanupErr)
			r.mu.Unlock()
		} else {
			p.released.Store(true)
		}
		return
	}
	p.client = mimic.NewClient(p, p.id)
	r.mu.Unlock()
}

func (r *Registry) detach(ctx context.Context, id string) error {
	_, err := r.transport.CallRaw(ctx, "Target.detachFromTarget", map[string]any{"sessionId": id}, "")
	var protocol *mimic.RPCError
	if errors.As(err, &protocol) && protocol.Code == -32000 && protocol.Message == "No session with given id" {
		return nil // A target-close event may race explicit detachment.
	}
	return err
}

func (r *Registry) Detach(ctx context.Context, target string) error {
	r.mu.Lock()
	if r.closed {
		r.mu.Unlock()
		return errClosed
	}
	p := r.pages[target]
	if p == nil {
		r.mu.Unlock()
		return nil
	}
	p.active.Store(false)
	r.workers.Add(1)
	r.mu.Unlock()
	defer r.workers.Done()
	if p.owned {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-p.ready:
		}
		if p.err != nil {
			return nil
		}
		if err := r.detach(ctx, p.id); err != nil {
			r.mu.Lock()
			r.cleanupErr = errors.Join(r.cleanupErr, err)
			r.mu.Unlock()
			return err
		}
		p.released.Store(true)
	}
	r.mu.Lock()
	if r.pages[target] == p {
		delete(r.pages, target)
	}
	r.mu.Unlock()
	return nil
}

func (r *Registry) Close(ctx context.Context) error {
	r.mu.Lock()
	if r.closing {
		r.mu.Unlock()
		<-r.closeDone
		return r.closeErr
	}
	r.closing = true
	pages := r.pages
	r.pages = make(map[string]*attachment)
	r.closed = true
	for _, p := range pages {
		p.active.Store(false)
	}
	r.mu.Unlock()
	r.unsubscribe()
	<-r.done
	r.cancel()
	r.workers.Wait()
	r.mu.Lock()
	result := r.cleanupErr
	r.mu.Unlock()
	for _, p := range pages {
		if p.owned && p.err == nil && !p.released.Load() {
			if err := r.detach(ctx, p.id); err != nil {
				result = errors.Join(result, fmt.Errorf("detach page extension: %w", err))
			}
		}
	}
	r.closeErr = result
	close(r.closeDone)
	return result
}
