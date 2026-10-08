// Command mimic-sdk performs deliberate runtime cache operations. It never
// launches a browser or downloads framework browser bundles.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	mimic "github.com/mimic-browser/sdk/go"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
func run() error {
	if len(os.Args) < 2 {
		return fmt.Errorf("usage: mimic-sdk install|lock|list|verify|prune [options]")
	}
	flags := flag.NewFlagSet(os.Args[1], flag.ContinueOnError)
	var options mimic.RuntimeOptions
	flags.StringVar(&options.Version, "version", "", "exact runtime release")
	flags.StringVar(&options.LockFile, "lock", "", "runtime lock file")
	flags.StringVar(&options.RuntimeDir, "runtime-dir", "", "shared installation root")
	flags.StringVar(&options.ArchivePath, "archive", "", "verified offline archive")
	directory := flags.String("directory", "", "installation directory for verify/prune")
	offline := flags.Bool("offline", false, "disable downloads")
	if err := flags.Parse(os.Args[2:]); err != nil {
		return err
	}
	if *offline {
		allow := false
		options.AllowDownload = &allow
	}
	manager := mimic.NewRuntimeManager(options)
	ctx := context.Background()
	var value any
	var err error
	switch os.Args[1] {
	case "install":
		value, err = manager.Install(ctx)
	case "lock":
		value, err = manager.ResolveLock(ctx)
	case "list":
		value, err = manager.List()
	case "verify", "prune":
		if *directory == "" {
			return fmt.Errorf("--directory is required")
		}
		installation, e := manager.Verify(*directory)
		if e != nil {
			return e
		}
		value = installation
		if os.Args[1] == "prune" {
			err = manager.Prune(ctx, *installation)
		}
	default:
		return fmt.Errorf("unknown command %q", os.Args[1])
	}
	if err != nil {
		return err
	}
	// CLI discovery includes the location; durable installation receipts keep
	// their shared cross-language format independent of the cache root.
	switch installation := value.(type) {
	case *mimic.Installation:
		value = installationOutput(installation)
	case []mimic.Installation:
		items := make([]any, 0, len(installation))
		for i := range installation {
			items = append(items, installationOutput(&installation[i]))
		}
		value = items
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	return encoder.Encode(value)
}

func installationOutput(installation *mimic.Installation) any {
	return struct {
		*mimic.Installation
		Path string `json:"path"`
	}{installation, installation.Path}
}
