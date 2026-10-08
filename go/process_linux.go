//go:build linux

package mimic

import (
	"errors"
	"os/exec"
	"syscall"
)

// The runtime owns parent-death observation. Linux Pdeathsig is scoped to the
// creating OS thread and is unsafe with Go's migrating goroutines.
func configureProcess(cmd *exec.Cmd) {}

func processAlive(pid int) bool { return !errors.Is(syscall.Kill(pid, 0), syscall.ESRCH) }
