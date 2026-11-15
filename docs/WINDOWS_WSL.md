# Windows Users: Run Avibe with WSL from Scratch

> The recommended way to run Avibe on Windows is the Desktop App: download it from [avibe.bot](https://avibe.bot/#download). This guide is for people who specifically want Avibe inside WSL, for example to keep their code and coding agents in a Linux environment.

This guide is written for Windows users who have never used WSL before.

If someone told you "use WSL" but you do not know where to install it, how to open it, or which window should run the commands, follow this guide step by step.

## Short Version

If you run Avibe in WSL, the setup is:

- `Windows`: browser, Slack, Discord, Telegram, WeChat, Lark/Feishu
- `WSL`: Avibe, Claude Code / Codex / OpenCode, your code repository

In plain terms:

- You still use Windows for your browser and chat apps
- But the commands and agent runtime run inside a Linux terminal window

That Linux terminal window is WSL.

## What Is WSL

WSL stands for `Windows Subsystem for Linux`.

You can think of it like this:

- Your computer is still Windows
- But Windows gives you a Linux command-line environment
- You can run many developer tools inside that Linux environment

For Avibe, which needs Python, Node, and agent CLIs, WSL is often smoother than running everything natively on Windows.

## You Will Use Two Different Windows

This is the part that confuses most first-time users.

### Window 1: PowerShell

PowerShell is only needed at the beginning to install WSL itself.

Think of it as:

- the Windows terminal used to install WSL

### Window 2: Ubuntu Terminal

The Ubuntu terminal is where you will actually install and run Avibe.

Think of it as:

- the Linux terminal used for daily Avibe usage

Commands like these:

```bash
curl ...
vibe
which codex
```

should be run in the `Ubuntu terminal`, not in PowerShell.

## Step 1: Install WSL

### 1. Open PowerShell

In Windows:

1. Open the Start menu
2. Search for `PowerShell`
3. Find `Windows PowerShell` or `PowerShell`
4. Right-click it
5. Choose `Run as administrator`

### 2. Run the WSL install command

In PowerShell, run:

```powershell
wsl --install
```

This command usually does two things:

- installs WSL
- installs a default Linux distribution, usually Ubuntu

If Windows asks you to restart, restart your computer.

### 3. After restart, look for Ubuntu

When installation is done, your Start menu will usually contain:

- `Ubuntu`

If you do not see it, search for:

- `Ubuntu`
- `Windows Terminal`

## Step 2: Launch Ubuntu for the First Time

### 1. Open Ubuntu

Open the Start menu and launch:

- `Ubuntu`

The first launch may take from a few seconds to a few minutes while it finishes Linux setup.

### 2. Create a Linux username and password

Ubuntu will ask you to create:

- a Linux username
- a Linux password

This is your `WSL Linux account`, not your Windows account.

It may look like this:

```text
Enter new UNIX username:
Enter new UNIX password:
```

After this step, your Ubuntu terminal is ready to use.

## Step 3: Confirm You Are in the WSL Terminal

When you see a prompt like this, you are usually inside Ubuntu:

```bash
yourname@DESKTOP-XXXX:~$
```

Typical signs are:

- your Linux username
- a tilde `~`
- a dollar sign `$`

From this point on, the remaining commands in this guide should be run in this Ubuntu terminal.

## Step 4: Install Basic Tools Inside Ubuntu

In the Ubuntu terminal, run:

```bash
sudo apt update
sudo apt install -y curl git
```

If it asks for a password, enter the Linux password you created earlier.

## Step 5: Create a Working Directory

It is better to keep your code inside WSL's Linux filesystem, not under your Windows `C:` drive mount.

In the Ubuntu terminal, run:

```bash
mkdir -p ~/work
cd ~/work
```

Recommended path:

```text
/home/<your-linux-username>/work
```

Not recommended for long-term daily use:

```text
/mnt/c/Users/...
```

That path usually has more performance and file-behavior edge cases.

## Step 6: Install Avibe

Now you are in the right place.

In the `Ubuntu terminal`, run:

```bash
curl -fsSL https://avibe.bot/install.sh | bash
```

Open source — view the [script on GitHub](https://github.com/avibe-bot/avibe/blob/master/install.sh). The short URL is a 307 redirect to that file.

After installation finishes, still in the same Ubuntu terminal, run:

```bash
vibe
```

Again, this command should be run:

- in the `Ubuntu terminal`
- not in PowerShell
- not in CMD

## Step 7: Open the Web UI in Your Windows Browser

After Avibe starts, the default Web UI address is:

```text
http://127.0.0.1:5123
```

Open that address in your normal Windows browser:

- Chrome
- Edge
- Firefox

If no browser opens automatically, open it manually and go to:

```text
http://127.0.0.1:5123
```

## Step 8: Install Agent CLIs Inside WSL

If you want to use Claude Code, Codex, or OpenCode, install those CLIs inside the `Ubuntu terminal` too.

Do not assume a Windows installation is enough.

Why:

- Avibe is running inside WSL
- it can only directly use commands available inside WSL

After installation, check from Ubuntu:

```bash
which claude
which codex
which opencode
```

If you see output like this, the CLI is available inside WSL:

```text
/home/yourname/.local/bin/codex
```

## Step 9: Set the Default Working Directory

In the Avibe Web UI, set the default working directory to a WSL path such as:

```text
/home/yourname/work
```

Avoid using these as your main default path:

```text
C:\Users\...
```

or:

```text
/mnt/c/Users/...
```

If you must access Windows files, `/mnt/c/...` can still be used temporarily, but it is better not to make it your long-term default.

## A Simple Daily Workflow

Here is the easiest mental model for daily usage.

### 1. Open Ubuntu

Start menu -> search `Ubuntu` -> open it

### 2. Go to your project

In the Ubuntu terminal:

```bash
cd ~/work/your-project
```

### 3. Start Avibe

In the Ubuntu terminal:

```bash
vibe
```

### 4. Open the browser

In your Windows browser, open:

```text
http://127.0.0.1:5123
```

### 5. Use your chat app normally

After that, continue using Slack, Discord, Telegram, WeChat, or Lark as usual.

## How to Open WSL Again Later

You do not need to reinstall anything every time.

Later, whenever you want to continue:

1. Open the Start menu
2. Search for `Ubuntu`
3. Open Ubuntu
4. Run:

```bash
cd ~/work/your-project
vibe
```

## Common Questions

### 1. Which window should run `vibe`?

Use the `Ubuntu terminal`, not PowerShell.

### 2. Which window should run `curl ... | bash`?

Use the `Ubuntu terminal`, not PowerShell.

### 3. Do I still need PowerShell later?

Usually only for the original WSL installation.

For day-to-day Avibe use, you will mostly work in Ubuntu.

### 4. What if `http://127.0.0.1:5123` does not open?

First check whether `vibe` is still running in the Ubuntu terminal.

If Avibe already exited, the page will not open.

You can check from Ubuntu:

```bash
vibe status
```

### 5. What if agent installation from the Web UI fails?

If Avibe runs in WSL and you use the Web UI from a Windows browser, some local security checks may make certain install buttons fail.

If that happens, install the CLI manually in the Ubuntu terminal, then go back to the Web UI and fill in the path or rerun detection.

### 6. Do I need Linux knowledge for this?

No.

For basic usage, you only need to know how to:

- open Ubuntu
- use `cd` to enter a folder
- run `vibe`
- open `http://127.0.0.1:5123` in your browser

That is enough to get started.

## Quick Checklist

If all of these are true, your WSL setup is working:

- you can open `Ubuntu` from the Start menu
- Ubuntu shows a prompt like `yourname@DESKTOP-XXXX:~$`
- you can run `vibe` inside Ubuntu
- you can open `http://127.0.0.1:5123` in your Windows browser
- `which codex`, `which claude`, and `which opencode` work inside Ubuntu

## Official References

If you want the Microsoft documentation:

- WSL install: <https://learn.microsoft.com/windows/wsl/install>
- WSL basic commands: <https://learn.microsoft.com/windows/wsl/basic-commands>
- WSL development environment: <https://learn.microsoft.com/windows/wsl/setup/environment>

## In Short

The WSL setup, start to finish:

- install WSL on Windows
- open Ubuntu
- install and run Avibe inside Ubuntu
- open `http://127.0.0.1:5123` from your Windows browser

If you do not need a Linux environment, use the Desktop App instead.
