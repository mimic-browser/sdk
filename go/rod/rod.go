// Package rod bootstraps the real Rod client against a pinned Mimic runtime.
package rod

import (
	"context"
	"fmt"
	"sync"
	"time"

	native "github.com/go-rod/rod"
	"github.com/go-rod/rod/lib/cdp"
	mimic "github.com/mimic-browser/sdk/go"
)

type connection struct {
	transport   *mimic.Transport
	events      chan *cdp.Event
	cancel      context.CancelFunc
	unsubscribe func()
}

func (c *connection) Call(ctx context.Context, sessionID, method string, params interface{}) ([]byte, error) {
	return c.transport.CallRaw(ctx, method, params, sessionID)
}
func (c *connection) Event() <-chan *cdp.Event { return c.events }

// Session.Browser is the actual Rod Browser. Close never calls Browser.close on
// an attached runtime; native incognito contexts created by NewContext are owned.
type Session struct {
	Browser    *native.Browser
	Mimic      *mimic.Client
	Runtime    *mimic.RuntimeProcess
	transport  *mimic.Transport
	connection *connection
	mu         sync.Mutex
	contexts   []*native.Browser
	closed     bool
}

func Connect(ctx context.Context, endpoint string) (*Session, error) {
	t, err := mimic.Dial(ctx, endpoint)
	if err != nil {
		return nil, err
	}
	s, err := attach(ctx, t)
	if err != nil {
		_ = t.Close()
		return nil, err
	}
	return s, nil
}
func Launch(ctx context.Context, options mimic.RuntimeOptions) (*Session, error) {
	p, err := mimic.NewRuntimeManager(options).Launch(ctx)
	if err != nil {
		return nil, err
	}
	s, err := attach(ctx, p.Transport)
	if err != nil {
		_ = p.Close()
		return nil, err
	}
	s.Runtime = p
	return s, nil
}
func attach(ctx context.Context, t *mimic.Transport) (*Session, error) {
	client := mimic.NewClient(t, "")
	identity, err := client.GetVersion(ctx, mimic.GetVersionParams{})
	if err != nil || identity.Version == "" {
		return nil, fmt.Errorf("Mimic identity failed: %v", err)
	}
	lifetime, cancel := context.WithCancel(ctx)
	events, unsubscribe := t.Subscribe()
	conn := &connection{transport: t, events: make(chan *cdp.Event), cancel: cancel, unsubscribe: unsubscribe}
	go func() {
		defer close(conn.events)
		for {
			select {
			case <-lifetime.Done():
				return
			case event, ok := <-events:
				if !ok {
					return
				}
				select {
				case conn.events <- &cdp.Event{SessionID: event.SessionID, Method: event.Method, Params: event.Params}:
				case <-lifetime.Done():
					return
				}
			}
		}
	}()
	b := native.New().Context(lifetime).Client(conn)
	if err = b.Connect(); err != nil {
		cancel()
		unsubscribe()
		return nil, err
	}
	return &Session{Browser: b, Mimic: client, transport: t, connection: conn}, nil
}

// NewContext returns a genuine Rod incognito Browser. Context creation and
// configuration remain isolated from any other client attached to the runtime.
func (s *Session) NewContext(ctx context.Context, media *mimic.MediaConfiguration, policy *mimic.ResourcePolicy) (*native.Browser, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return nil, fmt.Errorf("integration session closed")
	}
	b, err := s.Browser.Context(ctx).Incognito()
	if err != nil {
		return nil, err
	}
	id := string(b.BrowserContextID)
	if media != nil {
		_, err = s.Mimic.SetMediaProfile(ctx, mimic.SetMediaProfileParams{BrowserContextId: mimic.Some(id), Camera: media.Camera, Microphone: media.Microphone, Devices: media.Devices, Seed: media.Seed})
		if err != nil {
			_ = b.Close()
			return nil, err
		}
	}
	if policy != nil {
		_, err = s.Mimic.UpdateResourcePolicy(ctx, mimic.UpdateResourcePolicyParams{BrowserContextId: id, Policy: *policy})
		if err != nil {
			_ = b.Close()
			return nil, err
		}
	}
	s.contexts = append(s.contexts, b)
	return b, nil
}
func (s *Session) ForPage(page *native.Page) *mimic.Client {
	return mimic.NewClient(s.transport, string(page.SessionID))
}

// NewConfiguredContext installs a coherent Mimic profile before any user page
// exists and returns a real Rod incognito Browser. Requires configureContext.
func (s *Session) NewConfiguredContext(ctx context.Context, configuration mimic.ConfigureContextParams) (*native.Browser, error) {
	return s.newConfiguredContext(ctx, configuration, nil)
}

// NewConfiguredContextWithMedia discovers capture sources in the new Context
// before configuring the public device profile or creating any user Page.
func (s *Session) NewConfiguredContextWithMedia(ctx context.Context, configuration mimic.ConfigureContextParams, factory mimic.MediaFactory) (*native.Browser, error) {
	if factory == nil || configuration.Media.Set {
		return nil, fmt.Errorf("provide a media factory and leave configuration.Media unset")
	}
	return s.newConfiguredContext(ctx, configuration, factory)
}

func (s *Session) newConfiguredContext(ctx context.Context, configuration mimic.ConfigureContextParams, factory mimic.MediaFactory) (*native.Browser, error) {
	if configuration.BrowserContextId != "" {
		return nil, fmt.Errorf("context creation assigns BrowserContextId; leave it empty")
	}
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil, fmt.Errorf("integration session closed")
	}
	b, err := s.Browser.Context(ctx).Incognito()
	if err != nil {
		s.mu.Unlock()
		return nil, err
	}
	// Record ownership before invoking user code, which may cancel or Close.
	s.contexts = append(s.contexts, b)
	s.mu.Unlock()
	configured := false
	defer func() {
		if !configured {
			cleanupCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			if b.Context(cleanupCtx).Close() == nil {
				s.mu.Lock()
				for i, owned := range s.contexts {
					if owned == b {
						s.contexts = append(s.contexts[:i], s.contexts[i+1:]...)
						break
					}
				}
				s.mu.Unlock()
			}
		}
	}()
	configuration.BrowserContextId = string(b.BrowserContextID)
	if factory != nil {
		media, err := factory(ctx, mimic.ContextSetup{BrowserContextID: configuration.BrowserContextId, Mimic: s.Mimic})
		if err != nil {
			return nil, err
		}
		configuration.Media = mimic.Some(media)
	}
	if _, err = s.Mimic.ConfigureContext(ctx, configuration); err != nil {
		return nil, err
	}
	// Rod otherwise applies its default laptop device on every new Page.
	// The managed Mimic profile already owns these values.
	b.NoDefaultDevice()
	configured = true
	return b, nil
}
func (s *Session) Close() error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	contexts := s.contexts
	s.contexts = nil
	s.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	for _, b := range contexts {
		_ = b.Context(ctx).Close()
	}
	s.connection.cancel()
	s.connection.unsubscribe()
	if s.Runtime != nil {
		return s.Runtime.Close()
	}
	return s.transport.Close()
}
