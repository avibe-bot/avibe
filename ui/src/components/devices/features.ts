// Parts of the Devices pages that ship turned off in this release.

/**
 * 「更改域名」 is offered on 「连接方式」. Off until the runtime's rename fix (#4224) is in: then the
 * entry appears where the instance says the name is eligible, with no other change to the page.
 * Off, the page shows the domain alone and its rename address opens no dialog; the dialogs, their
 * frames and their tests stay as they are.
 */
export const RENAME_ENTRY_ENABLED = false;
