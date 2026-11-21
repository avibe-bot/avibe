# AV-13 terminal copy: `vibe remote rebind` and the commands around it

Every sentence a person reads at the terminal, with the state or code that
produces it. For review before the code is finished: Product reads the ones
marked **[Product]** (safety, loss, names); the copy reviewer reads the rest.
`{…}` is filled in at run time. Nothing here prints an installation id, a device
key, a callback proof or the reset confirmation.

The four I am least sure of are marked **[?1]** to **[?4]**. Open questions that
are not about wording are in the last section.

## How the command runs

`vibe remote rebind` takes no arguments. It reads the state of this computer,
prints section B, then:

| State of this computer | What the command does | Sections |
|---|---|---|
| bound, or bound and refused by avibe.bot | explains "Bind remote access again" (browser), then offers "Bind as a new computer" | C, D, then E, F |
| binding record damaged | offers "Bind as a new computer" only | C3, D, then E, F |
| not bound | binds it: link, wait, accept account and domain | E |
| an earlier attempt is unfinished | continues that attempt, starts nothing | H |
| a reset is unfinished | finishes it, then binds | H, then E |
| a reset cannot finish | says so and stops | H8 |

It never asks more than one question at a time, and every question declines on
Enter, on end of input and on Ctrl+C.

---

## A. Where it will not run

**A1** · not a terminal on both input and output, or started by an Agent, a Task
or a Watch (`local_operator_required`)

EN
```
This command asks questions and changes which Avibe this computer is bound to.
Run it by hand in a terminal on this computer; an SSH session works.
It does not run from an Agent, a Task, a script or a pipe.
Nothing changed.
```
ZH
```
这条命令会提问，并且会改变这台电脑绑定到哪个 Avibe。
请在这台电脑的终端里手动运行，SSH 登录也可以。
它不能由 Agent、定时任务、脚本或管道运行。
什么都没有改变。
```

---

## B. The state, printed first every time

**B1** · heading and lines

| Line | EN | ZH |
|---|---|---|
| heading | `Remote access on this computer` | `这台电脑的远程访问` |
| domain (when one is saved) | `  Domain:  {url}` | `  域名：{url}` |
| binding | `  Binding: {state}` | `  绑定：{state}` |
| tunnel | `  Tunnel:  running` / `stopped` | `  隧道：运行中` / `已停止` |

**B2** · `{state}` values

| Local state | EN | ZH |
|---|---|---|
| `unpaired` | `not bound` | `未绑定` |
| `bound` | `bound` | `已绑定` |
| `binding_rejected` | `refused by avibe.bot` | `avibe.bot 不再接受` |
| `pairing_recovery_required` | `an earlier attempt is unfinished` | `上一次绑定还没有完成` |
| `resetting` | `a reset is unfinished` | `重置还没有完成` |
| `reset_repair_required` | `a reset cannot finish` | `重置无法完成` |
| identity file, pending record or saved binding unreadable | `the binding record on this computer is damaged` | `这台电脑上的绑定记录已损坏` |

**B3** · under the lines when the state is `binding_rejected`. The second
sentence only when the tunnel process is still running.

EN
```
avibe.bot has refused this computer's binding at least twice in a row.
Either this Avibe was deleted on avibe.bot, or its binding was replaced from
another computer. This computer cannot tell which.
The tunnel is still running. That does not mean the binding is accepted.
```
ZH
```
avibe.bot 已经连续至少两次拒绝这台电脑的绑定。
可能是这个 Avibe 在 avibe.bot 上被删除了，也可能是它被另一台电脑重新绑定了；
这台电脑分辨不出是哪一种。
隧道还在运行，但这不代表绑定仍然被接受。
```

**B4** · settings file could not be read cleanly (local config recovery
refusal). The command stops here.

EN
```
Avibe could not read its settings file cleanly, and will not change the
binding on top of that. Nothing changed.
Run this, fix what it reports, then run vibe remote rebind again:
  vibe doctor
```
ZH
```
Avibe 没能完整读取它的设置文件，不会在这种情况下更改绑定。什么都没有改变。
请运行下面的命令，按提示修复后再运行 vibe remote rebind：
  vibe doctor
```

---

## C. The two ways, shown when the computer is bound

**C1** · heading

| EN | ZH |
|---|---|
| `There are two ways to bind this computer again.` | `重新绑定这台电脑有两种方式。` |

**C2** · **[Product]** the browser way. The third paragraph is Product's
sentence, unchanged. `{port}` is this computer's Avibe port.

EN
```
1. Bind remote access again
   For the Avibe this computer is bound to now. It is done in a browser that
   opens Avibe at this computer's own address, on the Devices page.
   On a server with no browser, forward the port from your own computer:
       ssh -L {port}:127.0.0.1:{port} <user>@<this-server>
   then open http://127.0.0.1:{port} there and go to Devices.

   Binding again signs every remote device out. Pages open on this computer
   are not affected. It does not change the tunnel credential.

   If someone may have copied this computer's Avibe data as a whole, delete
   this Avibe at avibe.bot, bind it again, then take your name back.
```
ZH
```
1. 重新绑定远程访问
   针对这台电脑现在绑定的 Avibe。要在浏览器里完成：用浏览器打开这台电脑
   本机地址上的 Avibe，进入「设备」页。
   服务器上没有浏览器时，从你自己的电脑做 SSH 端口转发：
       ssh -L {port}:127.0.0.1:{port} <用户>@<这台服务器>
   然后在你的电脑上打开 http://127.0.0.1:{port}，进入「设备」页。

   重新绑定会让所有远程设备退出登录；在这台电脑上打开的页面不受影响。
   它不会更换隧道凭证。

   如果这台电脑上的 Avibe 数据可能被人整个复制走过，请在 avibe.bot 上删除
   这个 Avibe，重新绑定后再把名字拿回来。
```

**C3** · the second way; the question in D follows directly

| When | EN | ZH |
|---|---|---|
| bound | `2. Bind as a new computer` | `2. 作为一台新电脑重新绑定` |
| binding record damaged (no way 1) | `The record of this computer's binding is damaged, so it cannot be bound again as it is. It can be bound as a new computer.` | `这台电脑上的绑定记录已损坏，没法照原样重新绑定。可以把它作为一台新电脑重新绑定。` |

---

## D. Bind as a new computer

**D1** · **[Product] [?1] [?2]** what is lost and kept, then the question.
Printed only after `reset_interactively` has accepted the state, before
anything is sealed. `{host}` is the saved domain; without one the first and
last "You lose" lines say "the current domain". The Health line is printed only
when Health data exists on this computer.

EN
```
Bind as a new computer

This computer forgets the Avibe it is bound to. After that it can be bound as a
new computer, to any account.

You lose:
  - Remote access through {host} stops at once.
  - Every device signed in from elsewhere is signed out, and notifications to
    those devices stop.
  - Everyone you gave access to loses it. The new binding starts with its
    owner only.
  - Every link already sent (Show Pages, files) stops working.
  - Health data already synced is deleted; connect Health again on the iPhone.
  - {host} does not move with this computer. See "Taking the old name back"
    at the end.

You keep:
  - Everything on this computer: sessions, files, Agents and settings.
    Pages open on this computer keep working.

This does not change:
  - The Avibe on avibe.bot is not deleted. If it still exists it keeps its
    name, and it can no longer reach this computer. If it belongs to another
    account, that account is not told.
  - No credential is changed: not the tunnel credential, not model keys, not
    the Vault.

If someone may have copied this computer's Avibe data as a whole, delete this
Avibe at avibe.bot, bind it again, then take your name back.

This cannot be undone.
Type yes to bind as a new computer. Press Enter to leave everything as it is:
```
ZH
```
作为一台新电脑重新绑定

这台电脑会忘掉它现在绑定的 Avibe，之后可以作为一台新电脑绑定到任何账号。

你会失去：
  - 通过 {host} 的远程访问立即停止。
  - 所有远程设备都会退出登录，发给这些设备的通知也会停止。
  - 你授权过的人都会失去访问权限；新的绑定一开始只有所有者自己。
  - 已经发出去的链接（展示页、文件）全部失效。
  - 已同步的健康数据会删除，要在 iPhone 上重新连接健康。
  - {host} 不会跟着这台电脑走，见最后的「把原来的名字拿回来」。

你会保留：
  - 这台电脑上的一切：会话、文件、Agent 和设置。
    在这台电脑上打开的页面照常可用。

这一步不会改变：
  - avibe.bot 上的那个 Avibe 不会被删除。它如果还在，名字仍然归它，只是
    再也连不到这台电脑。如果它属于别的账号，对方不会收到任何通知。
  - 不会更换任何凭证：隧道凭证、模型密钥、Vault 都不变。

如果这台电脑上的 Avibe 数据可能被人整个复制走过，请在 avibe.bot 上删除
这个 Avibe，重新绑定后再把名字拿回来。

这一步不能撤销。
输入 yes 作为一台新电脑重新绑定；直接按回车则一切保持原样：
```

**D2** · results of the question

| State / code | EN | ZH |
|---|---|---|
| anything but `yes`, end of input, Ctrl+C (`declined`) | `Nothing changed. This computer's binding is as it was.` | `什么都没有改变，这台电脑的绑定和之前一样。` |
| `{'state':'unpaired'}` | `Done. This computer is no longer bound to any Avibe. Everything on it is as it was.` | `完成。这台电脑不再绑定任何 Avibe，电脑上的内容原样保留。` |
| the binding changed while the question was open (`local_binding_changed`, nothing sealed) | `This computer's binding changed while you were answering, so nothing was reset. To see where it stands now:` + `  vibe remote rebind` | `在你回答期间，这台电脑的绑定发生了变化，所以没有重置。查看现在的状态：` + 同一条命令 |
| another binding or domain change holds the lock (`operation_in_progress`) | `Another change to this computer's binding or domain is running. Nothing changed. When it has finished, run:` + `  vibe remote rebind` | `这台电脑的绑定或域名正在进行另一项更改。什么都没有改变。等它完成后运行：` + 同一条命令 |
| any failure after the reset was sealed | `The reset started and did not finish. Remote access on this computer stays off until it does. To continue it:` + `  vibe remote rebind` | `重置已经开始，但没有完成。完成之前，这台电脑的远程访问保持关闭。继续请运行：` + 同一条命令 |
| `pairing_recovery_required` | G4 | G4 |

---

## E. Binding the computer (the ordinary link)

Reached when the computer is not bound: after D, after a finished reset, or
directly. No question is asked before the link is shown; nothing is bound
until E3 is accepted.

**E1** · before the link

EN
```
Bind this computer

Open a link on any device, sign in to avibe.bot and choose a domain there.
This terminal then shows the account and the domain and asks you to accept
them. Nothing is bound before that.
```
ZH
```
绑定这台电脑

在任意设备上打开一个链接，登录 avibe.bot 并在那里选好域名。
之后这个终端会显示账号和域名，请你确认。确认之前不会绑定任何东西。
```

**E2** · the link (`show_link`)

EN
```
Open this link:
  {url}
Waiting for it to be approved on avibe.bot. Ctrl+C stops.
```
ZH
```
打开这个链接：
  {url}
正在等待 avibe.bot 上的确认。按 Ctrl+C 停止。
```

**E3** · **[Product]** the account and the domain (`confirm(email, hostname)`)

EN
```
avibe.bot reports that this request was approved by:
  Account: {email}
  Domain:  {host}
This account becomes the owner of this computer's Avibe and can open
everything in it from anywhere. If the account or the domain is not what you
expect, answer no.
Bind this computer to this account at this domain? [y/N]
```
ZH
```
avibe.bot 显示，确认这次请求的是：
  账号：{email}
  域名：{host}
这个账号会成为这台电脑上 Avibe 的所有者，可以从任何地方打开其中的一切。
账号或域名和你预期的不一样，就回答 n。
把这台电脑绑定到这个账号和这个域名？[y/N]
```

**E4** · results

| State / code | EN | ZH |
|---|---|---|
| `bound`, tunnel running | `Bound. This computer's Avibe is at https://{host}, owned by {email}.` + `Sign in at that address on your other devices.` | `已绑定。这台电脑上的 Avibe 在 https://{host}，所有者是 {email}。` + `请在其他设备上打开这个地址重新登录。` |
| `bound`, tunnel not running | first line as above, then `The tunnel is not running yet. Start it with:` + `  vibe remote start` | 第一行同上，然后 `隧道还没有运行。启动它：` + `  vibe remote start` |
| `declined` at E3 | `Not bound. You answered no, so nothing was bound. To try again:` + `  vibe remote rebind` | `没有绑定。你回答了“否”，所以什么都没有绑定。再试一次：` + 同一条命令 |
| Ctrl+C before E3 was accepted | `Stopped. Nothing was bound; this computer is not bound. To continue:` + `  vibe remote rebind` | `已停止。什么都没有绑定，这台电脑现在是未绑定状态。继续请运行：` + 同一条命令 |
| Ctrl+C after E3 was accepted, no saved attempt | `Stopped after you accepted. This computer is not bound. avibe.bot may have recorded the request. Run this and approve once more:` + `  vibe remote rebind` | `在你确认之后停止了。这台电脑没有绑定；avibe.bot 那边可能已经记下了这次请求。请运行下面的命令并再确认一次：` + 同一条命令 |
| Ctrl+C after E3 was accepted, attempt saved | G4 | G4 |
| `expired` | `The link expired before it was approved. Nothing was bound. For a new link:` + `  vibe remote rebind` | `链接在确认之前过期了。什么都没有绑定。获取新链接：` + 同一条命令 |
| `already_bound` | `This computer is already bound. Nothing changed.` then section B again | `这台电脑已经绑定了。什么都没有改变。` 然后重新显示 B |

---

## F. Taking the old name back

**F1** · **[Product] [?3]** printed after E4 `bound` when this run reset the
computer. `{old}` is the domain saved before the reset; when none was saved the
last paragraph says "the old name". The 30 days appear only in the general
sentence, never attached to `{old}`.

EN
```
Taking the old name back

A name that is given up is kept for the account that had it: for 30 days plus
48 hours nobody else can claim it.
The old Avibe gives up its name when it is deleted on avibe.bot. Until then
the name stays with it.
To use {old} for this computer, open Devices in Avibe and choose
"Change the domain". avibe.bot decides whether the name can be taken; it is not
guaranteed.
```
ZH
```
把原来的名字拿回来

放弃的名字会为原来的账号保留：30 天加 48 小时之内，别人不能领取。
原来的 Avibe 在 avibe.bot 上被删除时才放弃它的名字；在那之前名字一直归它。
想让这台电脑用回 {old}：在 Avibe 的「设备」页选择「更改域名」。
能不能拿回由 avibe.bot 决定，不保证一定可以。
```

**F2** · printed instead of F1 when this run did not reset anything

| EN | ZH |
|---|---|
| `Had another name before? See "Taking the old name back" in: vibe remote rebind --help` | `之前用过别的名字？见 vibe remote rebind --help 里的 "Taking the old name back"。` |

---

## G. Refusals

Each says what is true, then the way out. "Nothing was bound" is used in the
link step, where the computer may already have been reset; "Nothing changed"
only where that is literally so.

**G1** · `active_pairing_recent`

EN
```
avibe.bot refused: that Avibe is connected to a computer that was active in
the last 15 minutes. Nothing was bound.
Go to that computer, or delete the Avibe on avibe.bot and bind again.
```
ZH
```
avibe.bot 拒绝了：那个 Avibe 正连着一台电脑，它在最近 15 分钟内还活跃。
什么都没有绑定。
请到那台电脑上操作，或者在 avibe.bot 上删除那个 Avibe 后重新绑定。
```

**G2** · **[?4]** `paired_installation_loopback_required`, followed by the
question in D (see open question 4)

EN
```
avibe.bot refused: this computer has been bound before, and a computer like
that is bound from a browser at its own address, not through a link.
Nothing was bound.
Use the browser on this computer, or from another computer through SSH port
forwarding:
    ssh -L {port}:127.0.0.1:{port} <user>@<this-server>
then open http://127.0.0.1:{port} there, go to Devices and turn on remote
access.

If you cannot do that, this computer can be bound as a new computer instead.
```
ZH
```
avibe.bot 拒绝了：这台电脑以前绑定过，这样的电脑要在它本机地址上的浏览器里
绑定，不能通过链接。什么都没有绑定。
请使用这台电脑上的浏览器，或者从另一台电脑做 SSH 端口转发：
    ssh -L {port}:127.0.0.1:{port} <用户>@<这台服务器>
然后在那台电脑上打开 http://127.0.0.1:{port}，进入「设备」页开启远程访问。

如果做不到，也可以把这台电脑作为一台新电脑重新绑定。
```

**G3** · network (`control_plane_unreachable`), and `rate_limited`

| Code | EN | ZH |
|---|---|---|
| `control_plane_unreachable` | `avibe.bot could not be reached, so nothing was bound. Check this computer's network, then run:` + `  vibe remote rebind` | `暂时连不上 avibe.bot，所以什么都没有绑定。检查这台电脑的网络后运行：` + 同一条命令 |
| `rate_limited` | `avibe.bot asked this computer to wait, so nothing was bound. Wait {seconds} seconds, then run:` + `  vibe remote rebind` | `avibe.bot 要求这台电脑稍后再试，所以什么都没有绑定。等 {seconds} 秒后运行：` + 同一条命令 |

**G4** · **[Product]** `pairing_recovery_required`

EN
```
The last attempt may have completed on avibe.bot, but this computer has not
finished saving it. Do not start another one.
To continue that same attempt:
  vibe remote rebind
```
ZH
```
上一次绑定可能已经在 avibe.bot 上完成，但这台电脑还没有把它保存完。
不要再开始一次新的绑定。
继续上一次请运行：
  vibe remote rebind
```

**G5** · other codes from the link step

| Code | EN | ZH |
|---|---|---|
| `fresh_claim_required` | `The reply from avibe.bot was lost after you accepted, and it cannot be asked for again. This computer is not bound. Run this and approve once more:` + `  vibe remote rebind` | `你确认之后，avibe.bot 的回复没有送到，而且无法重新获取。这台电脑没有绑定。请运行下面的命令并再确认一次：` + 同一条命令 |
| `claim_in_progress` | `Another request to bind this computer is already open, in Avibe on this computer or in another terminal. Finish or cancel that one first. Nothing was bound here.` | `这台电脑已经有一个绑定请求在进行（在这台电脑的 Avibe 里，或另一个终端里）。请先完成或取消那一个。这里什么都没有绑定。` |
| `operation_in_progress` | as in D2 | 同 D2 |
| `name_taken` | `That domain was taken before the binding finished. Nothing was bound. Run this and choose another:` + `  vibe remote rebind` | `绑定完成之前，这个域名被别人占用了。什么都没有绑定。请运行下面的命令并换一个：` + 同一条命令 |
| `binding_unavailable` with no reason **[?4]** | `avibe.bot refused to bind this computer to that account and gave no reason it allows this computer to show. Nothing was bound.` + `Sign in with the account this computer was bound to before, or bind it as a new computer.` then the question in D | `avibe.bot 拒绝把这台电脑绑定到那个账号，也没有给出可以显示的原因。什么都没有绑定。` + `请改用这台电脑之前绑定的账号登录，或者把它作为一台新电脑重新绑定。` 然后是 D 的提问 |
| `local_binding_changed` | `This computer's binding changed while this was running. Nothing more was done here. To see where it stands now:` + `  vibe remote rebind` | `运行期间这台电脑的绑定发生了变化，这里没有再做任何事。查看现在的状态：` + 同一条命令 |
| leftover records from an earlier binding are damaged (`local_binding_unavailable`, `local_instance_conflict`, `local_identity_unavailable`, `local_enrollment_unavailable`) | C3's damaged sentence, then the question in D | 同 C3 的“已损坏”一句，然后是 D 的提问 |
| anything else | `avibe.bot did not complete this request. Nothing was bound. To try again:` + `  vibe remote rebind` + `(code: {code})` | `avibe.bot 没有完成这次请求。什么都没有绑定。再试一次：` + 同一条命令 + `（代码：{code}）` |

---

## H. Continuing something unfinished

| State | When | EN | ZH |
|---|---|---|---|
| **H1** `pairing_recovery_required` | at start | `An earlier attempt to bind this computer may have completed on avibe.bot. Continuing that same attempt now; no new one is started.` | `之前的一次绑定可能已经在 avibe.bot 上完成。现在继续那一次，不会开始新的绑定。` |
| **H2** | it finished, now bound | `Finished. This computer is bound: {url}` | `已完成。这台电脑已绑定：{url}` |
| **H3** | it ended without binding | `That attempt did not complete and has been cleared. This computer is not bound.` then section E | `那一次绑定没有完成，记录已清除。这台电脑现在是未绑定状态。` 然后进入 E |
| **H4** | still unfinished | `It has not finished: avibe.bot could not be reached or has not answered yet. Nothing new was started. Avibe keeps retrying while it runs. To try again now:` + `  vibe remote rebind` | `还没有完成：暂时连不上 avibe.bot，或者它还没有答复。没有开始任何新的绑定。Avibe 运行期间会自动重试。现在再试一次：` + 同一条命令 |
| **H5** `resetting` | at start | `A reset of this computer's binding was started and has not finished. Finishing it now.` | `这台电脑的绑定重置已经开始，但还没有完成。现在把它做完。` |
| **H6** | it finished | D2 "Done" line, then section E | D2 的“完成”一句，然后进入 E |
| **H7** | still unfinished | `It could not finish yet. Remote access on this computer stays off until it does. To try again:` + `  vibe remote rebind` | `暂时还完成不了。完成之前，这台电脑的远程访问保持关闭。再试一次：` + 同一条命令 |
| **H8** `reset_repair_required` (see open question 5) | at start | `A reset of this computer's binding was started, and its record no longer matches this computer. Avibe will not continue it by guessing. Remote access stays off. Sessions, files and settings on this computer are not affected.` | `这台电脑的绑定重置已经开始，但它的记录和这台电脑现在的状态对不上。Avibe 不会靠猜测继续。远程访问保持关闭；这台电脑上的会话、文件和设置不受影响。` |

---

## I. Other `vibe remote` commands (English only)

`status`, `start` and `pair` are also run by Agents, so by the repository rule
their output stays English and is written in code.

| Where | State / code | Text |
|---|---|---|
| `vibe remote status`, new line | `binding_rejected` | `  Binding: refused by avibe.bot. See: vibe remote rebind` |
| | `pairing_recovery_required` | `  Binding: an earlier attempt is unfinished. Continue it: vibe remote rebind` |
| | `resetting` | `  Binding: a reset is unfinished. Continue it: vibe remote rebind` |
| | `reset_repair_required` | `  Binding: a reset cannot finish. See: vibe remote rebind` |
| `vibe remote start` | `binding_rejected` | `Remote access was not started: avibe.bot is refusing this computer's binding.` + `See what can be done: vibe remote rebind` |
| | `reset_in_progress` | `Remote access was not started: a reset of this computer's binding has not finished.` + `Continue it: vibe remote rebind` |
| `vibe remote pair`, success on a computer that was bound with remote access off | pairing ok, no tunnel start | `Pairing saved. Remote access was off on this computer and stays off.` (exit 0; today this prints "the tunnel did not start … unknown_error" and exits 1) |
| `vibe remote pair` | `operation_in_progress` | `Another change to this computer's binding or domain is running. No pairing key was used. When it has finished, run the command again.` |
| `vibe remote` on a bound computer, last lines (replaces "Need to switch account or domain? Run: vibe remote pair") | already bound | `To bind this computer again, or to another account:` + `  vibe remote rebind` + `To change the domain, open Devices in Avibe.` |

---

## J. Command help (English, read by people and Agents)

**J1** · `vibe remote --help`: one more line in the command list and in Examples

```
rebind    Bind this computer's remote access again (asks at the terminal)
```

**J2** · `vibe remote rebind --help`

```
usage: vibe remote rebind

Show which Avibe this computer is bound to, and bind it again. It asks its
questions at the terminal, so it runs by hand only: not from an Agent, a Task,
a script or a pipe. An SSH session works.

What it does depends on the state of this computer:
  bound         Explains "Bind remote access again", which is done in a browser,
                and offers "Bind as a new computer" here.
  refused       The same. avibe.bot refusing the binding does not say whether the
                Avibe was deleted or bound from another computer.
  not bound     Binds this computer: prints a link, waits for it to be approved
                on avibe.bot, then shows the account and the domain and asks
                you to accept them.
  unfinished    Continues the earlier attempt or reset. It never starts a
                second one.

Bind remote access again (browser)
  For the Avibe this computer is bound to now. Open Avibe at this computer's own
  address and go to Devices. On a server with no browser, forward the port from
  your own computer and open it there:
      ssh -L 5123:127.0.0.1:5123 <user>@<this-server>
      http://127.0.0.1:5123
  It signs every remote device out. Pages open on this computer are not
  affected. It does not change the tunnel credential.
  If someone may have copied this computer's Avibe data as a whole, delete this
  Avibe at avibe.bot, bind it again, then take your name back.

Bind as a new computer (this command)
  This computer forgets the Avibe it is bound to and is then bound as a new
  one. Remote access through the current domain stops, every remote device is
  signed out, everyone you gave access to loses it, links already sent stop
  working, and synced Health data is deleted. Everything on this computer is
  kept. The Avibe on avibe.bot is not deleted and no credential is changed.
  The command lists all of this and asks before it does anything.

Taking the old name back
  A name that is given up is kept for the account that had it: for 30 days plus
  48 hours nobody else can claim it. The old Avibe gives up its name when it is
  deleted on avibe.bot. To use the name for this computer, open Devices and
  choose "Change the domain". avibe.bot decides whether the name can be taken.

Examples:
  vibe remote rebind
```

**J3** · `vibe remote pair --help`: changed lines only

| Was | Now |
|---|---|
| help: `Pair directly when you already have a pairing key` | `Pair with a one-time pairing key from avibe.bot` |
| `This is the direct pairing command for users who already have a pairing key.` | `Use this when avibe.bot has given you a pairing key.` + `To bind a computer again, or to bind it to another account, use vibe remote rebind; it needs no key.` |
| `Pairing saves the remote-access config and then starts the managed tunnel automatically.` | `Pairing saves the binding and starts the tunnel. On a computer that was already bound it replaces that binding and signs every remote device out; if remote access was off there, it stays off.` |
| `The pairing key is one-time use; create a fresh key from the Avibe Cloud console if it fails.` | `A pairing key works once. If it fails, a new one is needed.` |

---

## Open questions (not wording)

1. **Language at the terminal.** `avibe/AGENTS.md` says the `vibe` CLI is
   Agent-facing, English, written in code, and never reads the configured
   language. The brief asks for the Runtime's strings in both languages. This
   one command refuses Agents and asks a person a destructive question, so I
   recommend the exception: sections A to H go in `vibe/i18n/en.json` and
   `zh.json`, the command prints the language configured in Avibe, and
   `--lang en|zh` overrides it; AGENTS.md gets one line recording the
   exception. Sections I and J stay English. Until this is decided the code
   prints English and carries the Chinese in the catalog.
2. **[?1] The reset is offered at the terminal without avibe.bot having
   refused, and without saying whose Avibe it is.** The brief says the reset is
   offered only after avibe.bot refused in one of the two ways that allow it,
   and that another account's hint is shown first. At the terminal neither can
   happen: binding a bound computer again is redirect-only with a loopback
   browser return (`_RebindFlow._execute`, wire "Start"), so the terminal never
   sees those refusals or the hint. The rule table's headless branch and
   `reset_interactively` allow the reset there on local state alone. D1 is
   written for that reading and names the other-account case in general words.
3. **[?2] Product's sentence inside D1.** It is used unchanged. In that place
   "bind it again" reads oddly, since the person is binding as new at that
   moment, and the sentence does not say why deleting matters there: a copied
   key still serves the old Avibe until it is deleted. A variant for D1 only:
   "If someone may have copied this computer's Avibe data as a whole, also
   delete the old Avibe at avibe.bot: a copy can still serve it until then."
   / 「如果这台电脑上的 Avibe 数据可能被人整个复制走过，还要在 avibe.bot 上删除
   原来的 Avibe：在那之前，拿到副本的人仍然可以用它。」
4. **[?4] Offering D after two link refusals.** After
   `paired_installation_loopback_required`, and after `binding_unavailable`
   with no reason, the text offers "Bind as a new computer". Neither refusal
   authorizes the reset in the browser flow. At the terminal the rule table
   says a headless machine that cannot give the loopback proof enrolls as new,
   so I offer it; without the offer the person on a server whose SSH forbids
   forwarding has no way out.
5. **`reset_repair_required` has no way out.** Nothing in the Runtime advances
   or clears a reset whose record no longer matches, and the hand-over forbids
   pointing at files. H8 says what is true and stops. It needs either a way
   out in the backend or a sentence naming where to get help.
6. **[?3] Where the name is taken back.** F1 points at Devices → "Change the
   domain". The rule table says the same account may also take a waiting name
   "on a new claim", that is, at the naming step on avibe.bot. I have not seen
   that screen; if it offers the name there, E1 should say so. Separately, the
   page's shipped rename copy says of the old name "You do not get it back"
   (`devices.rename.void`), which no longer matches the 30 days plus 48 hours
   rule.
