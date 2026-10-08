package mimic

import "context"

// ContextSetup exposes the new Context before configuration or any user Page.
// Source IDs must be discovered using this BrowserContextID as their scope.
type ContextSetup struct {
	BrowserContextID string
	Mimic            *Client
}

// MediaFactory selects private capture sources and declares independent public
// device identities. Adapters call it without holding their ownership mutex.
type MediaFactory func(context.Context, ContextSetup) (MediaConfiguration, error)
