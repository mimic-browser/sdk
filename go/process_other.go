//go:build !linux && !windows

package mimic

import "os/exec"

func configureProcess(cmd *exec.Cmd) {}

func processAlive(pid int) bool { return true }
