// Hosted-only test fixture. Query grants are adapted from the read-only
// signed-cu-acceptance Native.cs handoff (PR 3785); no product runtime code.
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;

public static class RefusalNative {
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool GetTokenInformation(IntPtr token, int kind, byte[] value, int length, out int needed);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool GetKernelObjectSecurity(IntPtr handle, uint info, byte[] descriptor, uint length, out uint needed);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool SetKernelObjectSecurity(IntPtr handle, uint info, byte[] descriptor);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr window);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr FindWindowEx(IntPtr parent, IntPtr after, string type, string title);
    [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr SendMessageTimeout(IntPtr window, uint message, IntPtr wparam, StringBuilder text, uint flags, uint timeout, out UIntPtr result);

    static byte[] TokenInfo(IntPtr token, int kind) {
        int needed;
        GetTokenInformation(token, kind, null, 0, out needed);
        if (needed <= 0 || needed > 65536) throw new InvalidOperationException("invalid token information length");
        byte[] bytes = new byte[needed];
        if (!GetTokenInformation(token, kind, bytes, bytes.Length, out needed)) throw new Win32Exception();
        return bytes;
    }
    public static bool IsElevated(int pid) {
        IntPtr process = OpenProcess(0x1000, false, pid), token = IntPtr.Zero;
        if (process == IntPtr.Zero) throw new Win32Exception();
        try {
            if (!OpenProcessToken(process, 8, out token)) throw new Win32Exception();
            return BitConverter.ToInt32(TokenInfo(token, 20), 0) != 0;
        } finally { if (token != IntPtr.Zero) CloseHandle(token); CloseHandle(process); }
    }
    static void GrantQuery(IntPtr handle, SecurityIdentifier sid, int mask) {
        uint needed;
        GetKernelObjectSecurity(handle, 4, null, 0, out needed);
        if (needed == 0 || needed > 65536) throw new InvalidOperationException("fixture DACL unreadable");
        byte[] bytes = new byte[needed];
        if (!GetKernelObjectSecurity(handle, 4, bytes, needed, out needed)) throw new Win32Exception();
        var descriptor = new RawSecurityDescriptor(bytes, 0);
        var acl = descriptor.DiscretionaryAcl;
        if (acl == null) return;
        int index = 0;
        while (index < acl.Count && (acl[index].AceFlags & AceFlags.Inherited) == 0) index++;
        acl.InsertAce(index, new CommonAce(AceFlags.None, AceQualifier.AccessAllowed, mask, sid, false, null));
        bytes = new byte[descriptor.BinaryLength];
        descriptor.GetBinaryForm(bytes, 0);
        if (!SetKernelObjectSecurity(handle, 4, bytes)) throw new Win32Exception();
    }
    public static void GrantFixtureQuery(IntPtr process, string sid) {
        if (process == IntPtr.Zero || process == new IntPtr(-1)) throw new InvalidOperationException("owned process handle required");
        IntPtr token = IntPtr.Zero, own = IntPtr.Zero;
        try {
            if (!OpenProcessToken(process, 8 | 0x20000 | 0x40000, out token) || !OpenProcessToken(new IntPtr(-1), 8, out own)) throw new Win32Exception();
            if (BitConverter.ToInt64(TokenInfo(token, 10), 0) == BitConverter.ToInt64(TokenInfo(own, 10), 0)) throw new InvalidOperationException("target token shared with parent");
            var reader = new SecurityIdentifier(sid);
            GrantQuery(token, reader, 8); // TOKEN_QUERY, never mutation rights
            GrantQuery(process, reader, 0x1000); // PROCESS_QUERY_LIMITED_INFORMATION
        } finally { if (own != IntPtr.Zero) CloseHandle(own); if (token != IntPtr.Zero) CloseHandle(token); }
    }
    public static string ReadEditor(IntPtr window, int pid) {
        uint owner;
        if (!IsWindow(window) || GetWindowThreadProcessId(window, out owner) == 0 || owner != (uint)pid) throw new InvalidOperationException("wrong fixture HWND owner");
        IntPtr edit = FindWindowEx(window, IntPtr.Zero, "EDIT", null);
        if (edit == IntPtr.Zero || FindWindowEx(window, edit, "EDIT", null) != IntPtr.Zero) throw new InvalidOperationException("expected exactly one EDIT");
        if (GetWindowThreadProcessId(edit, out owner) == 0 || owner != (uint)pid) throw new InvalidOperationException("wrong editor owner");
        var text = new StringBuilder(4096);
        UIntPtr count;
        if (SendMessageTimeout(edit, 0xD, new IntPtr(text.Capacity), text, 2, 2000, out count) == IntPtr.Zero) throw new Win32Exception();
        if (count.ToUInt64() >= (ulong)text.Capacity - 1) throw new InvalidOperationException("editor text truncated");
        return text.ToString();
    }
}
