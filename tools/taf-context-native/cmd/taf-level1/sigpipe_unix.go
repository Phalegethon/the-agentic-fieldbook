//go:build aix || darwin || dragonfly || freebsd || linux || netbsd || openbsd || solaris

package main

import (
	"os/signal"
	"syscall"
)

// installSIGPIPEHandling lets the bounded encoder observe a closed stdout and
// report its stable output failure instead of terminating by signal.
func installSIGPIPEHandling() { signal.Ignore(syscall.SIGPIPE) }
