// Package chromedp attaches the genuine chromedp allocator/context API to Mimic.
package chromedp

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/chromedp/cdproto/target"
	native "github.com/chromedp/chromedp"
	mimic "github.com/mimic-browser/sdk/go"
)

type Session struct {
	Context         context.Context
	Browser         *native.Browser
	Mimic           *mimic.Client
	Runtime         *mimic.RuntimeProcess
	transport       *mimic.Transport
	allocatorCancel context.CancelFunc
	contextCancel   context.CancelFunc
	once            sync.Once
	contextID       string
}

func Connect(ctx context.Context, endpoint string, options ...native.ContextOption) (*Session, error) {
	return connect(ctx, endpoint, nil, nil, options...)
}

// ConnectConfigured applies a managed profile before the native target exists.
func ConnectConfigured(ctx context.Context, endpoint string, configuration mimic.ConfigureContextParams, options ...native.ContextOption) (*Session, error) {
	return connect(ctx, endpoint, &configuration, nil, options...)
}

// ConnectConfiguredWithMedia selects Context-local capture sources before the
// native target exists. Closing the returned Session preserves the shared host.
func ConnectConfiguredWithMedia(ctx context.Context, endpoint string, configuration mimic.ConfigureContextParams, factory mimic.MediaFactory, options ...native.ContextOption) (*Session, error) {
	if factory == nil || configuration.Media.Set {
		return nil, fmt.Errorf("provide a media factory and leave configuration.Media unset")
	}
	return connect(ctx, endpoint, &configuration, factory, options...)
}

func connect(ctx context.Context, endpoint string, configuration *mimic.ConfigureContextParams, factory mimic.MediaFactory, options ...native.ContextOption) (*Session, error) {
	t, err := mimic.Dial(ctx, endpoint)
	if err != nil {
		return nil, err
	}
	s, err := attach(ctx, endpoint, t, configuration, factory, options...)
	if err != nil {
		_ = t.Close()
	}
	return s, err
}
func Launch(ctx context.Context, options mimic.RuntimeOptions, contextOptions ...native.ContextOption) (*Session, error) {
	return launch(ctx, options, nil, nil, contextOptions...)
}

// LaunchConfigured creates a coherent native chromedp context on an owned
// runtime. The current configureContext command must be supported by the runtime.
func LaunchConfigured(ctx context.Context, options mimic.RuntimeOptions, configuration mimic.ConfigureContextParams, contextOptions ...native.ContextOption) (*Session, error) {
	return launch(ctx, options, &configuration, nil, contextOptions...)
}

// LaunchConfiguredWithMedia configures private capture sources and public device
// identities independently, before creating the native chromedp target.
func LaunchConfiguredWithMedia(ctx context.Context, options mimic.RuntimeOptions, configuration mimic.ConfigureContextParams, factory mimic.MediaFactory, contextOptions ...native.ContextOption) (*Session, error) {
	if factory == nil || configuration.Media.Set {
		return nil, fmt.Errorf("provide a media factory and leave configuration.Media unset")
	}
	return launch(ctx, options, &configuration, factory, contextOptions...)
}

func launch(ctx context.Context, options mimic.RuntimeOptions, configuration *mimic.ConfigureContextParams, factory mimic.MediaFactory, contextOptions ...native.ContextOption) (*Session, error) {
	p, err := mimic.NewRuntimeManager(options).Launch(ctx)
	if err != nil {
		return nil, err
	}
	s, err := attach(ctx, p.Endpoint, p.Transport, configuration, factory, contextOptions...)
	if err != nil {
		_ = p.Close()
		return nil, err
	}
	s.Runtime = p
	return s, nil
}
func attach(ctx context.Context, endpoint string, t *mimic.Transport, configuration *mimic.ConfigureContextParams, factory mimic.MediaFactory, options ...native.ContextOption) (*Session, error) {
	if configuration != nil && configuration.BrowserContextId != "" {
		return nil, fmt.Errorf("context creation assigns BrowserContextId; leave it empty")
	}
	client := mimic.NewClient(t, "")
	identity, err := client.GetVersion(ctx, mimic.GetVersionParams{})
	if err != nil || identity.Version == "" {
		return nil, fmt.Errorf("Mimic identity failed: %v", err)
	}
	ws, err := mimic.Discover(ctx, endpoint)
	if err != nil {
		return nil, err
	}
	allocator, allocatorCancel := native.NewRemoteAllocator(ctx, ws)
	var owned struct {
		BrowserContextID string `json:"browserContextId"`
	}
	if err = client.Call(ctx, "Target.createBrowserContext", map[string]any{"disposeOnDetach": true}, &owned); err != nil {
		allocatorCancel()
		return nil, err
	}
	cleanup := func() {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = client.Call(cleanupCtx, "Target.disposeBrowserContext", map[string]any{"browserContextId": owned.BrowserContextID}, nil)
	}
	if configuration != nil {
		parameters := *configuration
		parameters.BrowserContextId = owned.BrowserContextID
		if factory != nil {
			media, factoryErr := factory(ctx, mimic.ContextSetup{BrowserContextID: owned.BrowserContextID, Mimic: client})
			if factoryErr != nil {
				cleanup()
				allocatorCancel()
				return nil, factoryErr
			}
			parameters.Media = mimic.Some(media)
		}
		if _, err = client.ConfigureContext(ctx, parameters); err != nil {
			cleanup()
			allocatorCancel()
			return nil, err
		}
	}
	var created struct {
		TargetID string `json:"targetId"`
	}
	if err = client.Call(ctx, "Target.createTarget", map[string]any{"url": "about:blank", "browserContextId": owned.BrowserContextID}, &created); err != nil {
		cleanup()
		allocatorCancel()
		return nil, err
	}
	// Attach through chromedp's public existing-target path. It hydrates the
	// already loaded about:blank frame before navigation listeners are installed.
	page, pageCancel := native.NewContext(allocator, append(options, native.WithTargetID(target.ID(created.TargetID)))...)
	if err = native.Run(page); err != nil {
		pageCancel()
		allocatorCancel()
		cleanup()
		return nil, err
	}
	return &Session{Context: page, Browser: native.FromContext(page).Browser, Mimic: client, transport: t, allocatorCancel: allocatorCancel, contextCancel: pageCancel, contextID: owned.BrowserContextID}, nil
}

// ForPage attaches the extension connection to a real chromedp target. Session
// IDs belong to a CDP connection, so another client's session ID is never reused.
func (s *Session) ForPage(ctx context.Context) (*mimic.Client, error) {
	page := native.FromContext(ctx)
	if page == nil || page.Target == nil {
		return nil, fmt.Errorf("chromedp target is not initialized")
	}
	var result struct {
		SessionID string `json:"sessionId"`
	}
	if err := s.Mimic.Call(ctx, "Target.attachToTarget", map[string]any{"targetId": page.Target.TargetID, "flatten": true}, &result); err != nil {
		return nil, err
	}
	return mimic.NewClient(s.transport, result.SessionID), nil
}
func (s *Session) Close() error {
	s.once.Do(func() {
		s.contextCancel()
		s.allocatorCancel()
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = s.Mimic.Call(ctx, "Target.disposeBrowserContext", map[string]any{"browserContextId": s.contextID}, nil)
		if s.Runtime != nil {
			_ = s.Runtime.Close()
		} else {
			_ = s.transport.Close()
		}
	})
	return nil
}
