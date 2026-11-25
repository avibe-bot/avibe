/**
 * avibe.bot's Account security page: where an account turns on two-step verification and sets
 * or changes its password. Both apps open it outside themselves (iPhone: Safari; Desktop: the
 * default browser).
 */
export const ACCOUNT_SECURITY_URL = 'https://avibe.bot/app/account/security';
/** The same page at its 「退出其他设备」 (Signed-in devices) section. */
export const ACCOUNT_SIGN_OUT_OTHERS_URL = `${ACCOUNT_SECURITY_URL}#sessions`;
