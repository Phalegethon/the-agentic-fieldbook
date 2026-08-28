//go:build windows

package boundary

import (
	"os"
	"syscall"
	"unsafe"
)

const (
	ownerSecurityInformation = 0x00000001
	daclSecurityInformation  = 0x00000004
	seFileObject             = 1
	accessAllowedAceType     = 0
	accessDeniedAceType      = 1
	aclSizeInformationClass  = 2
)

var (
	advapi32                   = syscall.NewLazyDLL("advapi32.dll")
	kernel32                   = syscall.NewLazyDLL("kernel32.dll")
	getNamedSecurityInfoW      = advapi32.NewProc("GetNamedSecurityInfoW")
	getSecurityInfo            = advapi32.NewProc("GetSecurityInfo")
	getSecurityDescriptorOwner = advapi32.NewProc("GetSecurityDescriptorOwner")
	getSecurityDescriptorDacl  = advapi32.NewProc("GetSecurityDescriptorDacl")
	getAclInformation          = advapi32.NewProc("GetAclInformation")
	getAce                     = advapi32.NewProc("GetAce")
	equalSid                   = advapi32.NewProc("EqualSid")
	convertSidToStringSidW     = advapi32.NewProc("ConvertSidToStringSidW")
	localFree                  = kernel32.NewProc("LocalFree")
)

type aclSizeInformation struct {
	AceCount      uint32
	AclBytesInUse uint32
	AclBytesFree  uint32
}

type aceHeader struct {
	AceType  byte
	AceFlags byte
	AceSize  uint16
}

func ownerOnly(root *os.Root) error {
	file, err := root.Open(".")
	if err != nil {
		return ErrUnsafeRoot
	}
	defer file.Close()
	return ownerOnlyOpenFile(file)
}

func ownerOnlyPath(path string) error {
	wide, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return ErrUnsafeRoot
	}
	var descriptor uintptr
	result, _, _ := getNamedSecurityInfoW.Call(uintptr(unsafe.Pointer(wide)), seFileObject, ownerSecurityInformation|daclSecurityInformation, 0, 0, 0, 0, uintptr(unsafe.Pointer(&descriptor)))
	if result != 0 || descriptor == 0 {
		return ErrUnsafeRoot
	}
	defer localFree.Call(descriptor)
	return validateOwnerOnlyACL(descriptor)
}

func ownerOnlyOpenFile(file *os.File) error {
	connection, err := file.SyscallConn()
	if err != nil {
		return ErrUnsafeRoot
	}
	var handle uintptr
	if err := connection.Control(func(value uintptr) { handle = value }); err != nil || handle == 0 {
		return ErrUnsafeRoot
	}
	var descriptor uintptr
	result, _, _ := getSecurityInfo.Call(handle, seFileObject, ownerSecurityInformation|daclSecurityInformation, 0, 0, 0, 0, uintptr(unsafe.Pointer(&descriptor)))
	if result != 0 || descriptor == 0 {
		return ErrUnsafeRoot
	}
	defer localFree.Call(descriptor)
	return validateOwnerOnlyACL(descriptor)
}

func validateOwnerOnlyACL(descriptor uintptr) error {
	var owner uintptr
	var ownerDefaulted int32
	result, _, _ := getSecurityDescriptorOwner.Call(descriptor, uintptr(unsafe.Pointer(&owner)), uintptr(unsafe.Pointer(&ownerDefaulted)))
	if result == 0 || owner == 0 {
		return ErrUnsafeRoot
	}
	var present, defaulted int32
	var dacl uintptr
	result, _, _ = getSecurityDescriptorDacl.Call(descriptor, uintptr(unsafe.Pointer(&present)), uintptr(unsafe.Pointer(&dacl)), uintptr(unsafe.Pointer(&defaulted)))
	if result == 0 || present == 0 || dacl == 0 {
		return ErrUnsafeRoot
	}
	var details aclSizeInformation
	result, _, _ = getAclInformation.Call(dacl, uintptr(unsafe.Pointer(&details)), unsafe.Sizeof(details), aclSizeInformationClass)
	if result == 0 {
		return ErrUnsafeRoot
	}
	for index := uint32(0); index < details.AceCount; index++ {
		var ace unsafe.Pointer
		result, _, _ = getAce.Call(dacl, uintptr(index), uintptr(unsafe.Pointer(&ace)))
		if result == 0 || ace == nil {
			return ErrUnsafeRoot
		}
		header := (*aceHeader)(ace)
		if header.AceType == accessDeniedAceType {
			continue
		}
		if header.AceType != accessAllowedAceType || header.AceSize < 12 {
			return ErrUnsafeRoot
		}
		sid := uintptr(unsafe.Add(ace, 8))
		if sidEqual(sid, owner) || privilegedSystemSID(sid) {
			continue
		}
		return ErrUnsafeRoot
	}
	return nil
}

func sidEqual(first, second uintptr) bool {
	result, _, _ := equalSid.Call(first, second)
	return result != 0
}

// SYSTEM and local Administrators are host principals, not other users. They
// are required by normal owner-created Windows directories and cannot grant
// access to an arbitrary user account.
func privilegedSystemSID(sid uintptr) bool {
	var text *uint16
	result, _, _ := convertSidToStringSidW.Call(sid, uintptr(unsafe.Pointer(&text)))
	if result == 0 || text == nil {
		return false
	}
	defer localFree.Call(uintptr(unsafe.Pointer(text)))
	value := syscall.UTF16ToString(unsafe.Slice(text, 184))
	return value == "S-1-5-18" || value == "S-1-5-32-544"
}

func ownerOnlyFile(info os.FileInfo) error { return ErrUnsafeRoot }

func safeStateFile(file *os.File) error { return ownerOnlyOpenFile(file) }
