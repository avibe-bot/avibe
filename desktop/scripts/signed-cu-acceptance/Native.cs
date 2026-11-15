// Test observer/controller only. It never impersonates the shipped CUA driver.
// Native menu automation invokes discovered user-visible menu commands; input
// into the target app is exclusively through the shipped vibe computer SDK.
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

public static class AcceptanceNative {
    private delegate bool EnumProc(IntPtr window, IntPtr parameter);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumProc callback, IntPtr parameter);
    [DllImport("user32.dll")] private static extern bool EnumChildWindows(IntPtr window, EnumProc callback, IntPtr parameter);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetWindowText(IntPtr window, StringBuilder text, int max);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetClassName(IntPtr window, StringBuilder text, int max);
    [DllImport("user32.dll")] private static extern IntPtr GetMenu(IntPtr window);
    [DllImport("user32.dll")] private static extern int GetMenuItemCount(IntPtr menu);
    [DllImport("user32.dll")] private static extern IntPtr GetSubMenu(IntPtr menu, int position);
    [DllImport("user32.dll")] private static extern uint GetMenuItemID(IntPtr menu, int position);
    [DllImport("user32.dll")] private static extern uint GetMenuState(IntPtr menu, uint item, uint flags);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetMenuString(IntPtr menu, uint item, StringBuilder text, int max, uint flags);
    [DllImport("user32.dll", SetLastError=true)] private static extern bool PostMessage(IntPtr window, uint message, IntPtr wparam, IntPtr lparam);
    [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)] private static extern IntPtr SendMessageTimeout(IntPtr window, uint message, IntPtr wparam, StringBuilder lparam, uint flags, uint timeout, out UIntPtr result);
    [DllImport("kernel32.dll", SetLastError=true)] private static extern IntPtr OpenProcess(uint rights, bool inherit, int pid);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
    [DllImport("advapi32.dll", SetLastError=true)] private static extern bool OpenProcessToken(IntPtr process, uint rights, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError=true)] private static extern bool GetTokenInformation(IntPtr token, int kind, out int value, int length, out int returned);

    public static bool IsElevated(int pid) {
        IntPtr process = OpenProcess(0x1000, false, pid);
        if (process == IntPtr.Zero) throw new Win32Exception();
        IntPtr token = IntPtr.Zero;
        try {
            if (!OpenProcessToken(process, 8, out token)) throw new Win32Exception();
            int elevated, size;
            if (!GetTokenInformation(token, 20, out elevated, 4, out size) || size != 4) throw new Win32Exception();
            return elevated != 0;
        } finally { if (token != IntPtr.Zero) CloseHandle(token); CloseHandle(process); }
    }
    private static string Label(IntPtr window) {
        var text = new StringBuilder(512);
        GetWindowText(window, text, text.Capacity);
        return text.ToString().Replace("&", "");
    }
    private static uint MenuCommand(IntPtr menu, string prefix, bool requireChecked) {
        for (int i=0; i<GetMenuItemCount(menu); i++) {
            IntPtr submenu = GetSubMenu(menu, i);
            if (submenu != IntPtr.Zero) {
                uint nested = MenuCommand(submenu, prefix, requireChecked);
                if (nested != UInt32.MaxValue) return nested;
            }
            var text = new StringBuilder(512);
            GetMenuString(menu, (uint)i, text, text.Capacity, 0x400);
            string label = text.ToString().Replace("&", "").Split('\t')[0];
            if (!label.StartsWith(prefix, StringComparison.Ordinal)) continue;
            uint state = GetMenuState(menu, (uint)i, 0x400);
            if ((state & 3) != 0 || (requireChecked && (state & 8) == 0)) throw new InvalidOperationException("native_menu_not_enabled_or_checked");
            uint id = GetMenuItemID(menu, i);
            if (id != UInt32.MaxValue) return id;
        }
        return UInt32.MaxValue;
    }
    public static void InvokeMenu(IntPtr window, string prefix, bool requireChecked) {
        uint id = MenuCommand(GetMenu(window), prefix, requireChecked);
        if (id == UInt32.MaxValue) throw new InvalidOperationException("native_menu_item_not_found");
        if (!PostMessage(window, 0x111, new IntPtr(id), IntPtr.Zero)) throw new Win32Exception();
    }
    public static bool ClickDialogButton(int pid, string label) {
        IntPtr button = IntPtr.Zero;
        EnumWindows((window, unused) => {
            uint owner; GetWindowThreadProcessId(window, out owner);
            var type = new StringBuilder(128); GetClassName(window, type, type.Capacity);
            if (owner != pid || type.ToString() != "#32770") return true;
            EnumChildWindows(window, (child, ignored) => {
                var childType = new StringBuilder(128); GetClassName(child, childType, childType.Capacity);
                if (childType.ToString() == "Button" && Label(child) == label) button = child;
                return true;
            }, IntPtr.Zero);
            return true;
        }, IntPtr.Zero);
        if (button == IntPtr.Zero) return false;
        if (!PostMessage(button, 0xF5, IntPtr.Zero, IntPtr.Zero)) throw new Win32Exception();
        return true;
    }
    public static bool UnsavedTextEquals(IntPtr window, string expected) {
        bool found = false;
        bool readFailed = false;
        EnumChildWindows(window, (child, unused) => {
            var type = new StringBuilder(128); GetClassName(child, type, type.Capacity);
            if (type.ToString() != "Edit" && !type.ToString().StartsWith("RichEdit", StringComparison.OrdinalIgnoreCase)) return true;
            var text = new StringBuilder(16384); UIntPtr count;
            if (SendMessageTimeout(child, 0xD, new IntPtr(text.Capacity), text, 2, 2000, out count) == IntPtr.Zero) { readFailed = true; return false; }
            if (text.ToString() == expected) found = true;
            return true;
        }, IntPtr.Zero);
        if (readFailed) throw new InvalidOperationException("native_edit_text_read_failed");
        return found;
    }
}
