// A real, test-owned Win32 EDIT control. No input or fake driver responses.
// Compiled only on disposable hosted Windows with the inbox Framework csc.
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using System.IO;

public sealed class RefusalEditor : Form {
    [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateWindowEx(uint exStyle, string type, string title, uint style,
        int x, int y, int width, int height, IntPtr parent, IntPtr menu, IntPtr instance, IntPtr param);
    [DllImport("user32.dll")] static extern IntPtr SetFocus(IntPtr window);
    IntPtr edit;
    readonly string receipt;
    EditObserver observer;
    // Counts actual mutation/input messages delivered to the native EDIT,
    // independently of SDK return values. WM_GETTEXT observation is not input.
    sealed class EditObserver : NativeWindow {
        readonly string receipt;
        int inputs;
        public EditObserver(IntPtr handle, string path) {
            receipt = path; AssignHandle(handle); Save();
        }
        void Save() { File.WriteAllText(receipt, "{\"input_messages\":" + inputs + "}"); }
        protected override void WndProc(ref Message m) {
            if (m.Msg == 0x100 || m.Msg == 0x102 || m.Msg == 0x109 ||
                m.Msg == 0xC || m.Msg == 0xC2 || m.Msg == 0x302) {
                inputs++; Save();
            }
            base.WndProc(ref m);
        }
    }
    public RefusalEditor(string path) {
        receipt = path;
        Text = "Avibe hosted refusal transport fixture";
        Width = 640; Height = 360;
        StartPosition = FormStartPosition.CenterScreen;
    }
    protected override void OnHandleCreated(EventArgs e) {
        base.OnHandleCreated(e);
        // WS_CHILD | WS_VISIBLE | WS_BORDER | ES_MULTILINE | ES_AUTOVSCROLL.
        edit = CreateWindowEx(0, "EDIT", "", 0x50800044, 10, 10, 590, 290,
            Handle, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero);
        if (edit == IntPtr.Zero) throw new Win32Exception();
        observer = new EditObserver(edit, receipt);
    }
    protected override void OnActivated(EventArgs e) { base.OnActivated(e); SetFocus(edit); }
    protected override void OnGotFocus(EventArgs e) { base.OnGotFocus(e); SetFocus(edit); }
    [STAThread] public static void Main(string[] args) {
        if (args.Length != 1) throw new ArgumentException("owned input receipt path required");
        Application.SetUnhandledExceptionMode(UnhandledExceptionMode.ThrowException);
        Application.EnableVisualStyles();
        Application.Run(new RefusalEditor(args[0]));
    }
}
