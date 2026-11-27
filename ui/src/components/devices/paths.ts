// Where the Devices pages live. The path keeps its old name: the claim's return, the Vault hints
// and the installed App's route memory already point there; only the label says 「设备」.
export const DEVICES_PATH = '/settings/remote-access';
export const DEVICES_ACTIVITY_PATH = `${DEVICES_PATH}/activity`;
export const CONNECTION_PATH = `${DEVICES_PATH}/connection`;
/** The connection page with 「更改域名」 open: reopening it comes back to a change in progress. */
export const CONNECTION_ADDRESS_PATH = `${CONNECTION_PATH}/address`;
/** Where a link to the bind-again row lands. */
export const CONNECTION_REBIND_HASH = '#rebind';
/** Setup's optional step, after the wizard's last screen. */
export const SETUP_REMOTE_ACCESS_PATH = '/setup/remote-access';

/** Where Avibe Desktop is downloaded (it picks the right build), and, signed in, where a person's
 *  Avibes are listed. */
export const AVIBE_DOWNLOAD_URL = 'https://avibe.bot/#download';
export const AVIBE_BOT_APP_URL = 'https://avibe.bot/app';
/** The address a phone opens this Avibe at. */
export const instanceUrl = (host: string) => `https://${host}`;

/** Navigation state the Devices page reads once: the unbinding just finished was binding as a new
 *  computer whose Avibe was another account's, so its name stays with the original account. */
export const NAME_WITH_ORIGINAL_ACCOUNT = 'unbindNameWithOriginalAccount';
