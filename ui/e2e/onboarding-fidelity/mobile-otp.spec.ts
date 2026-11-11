import { expect, test } from '../support/browserTest';
import { ORIGIN } from './support';

for (const colorScheme of ['light', 'dark'] as const) {
  test(`AUTH-SETUP-148 mobile OTP stays invisible while verification locks the input (${colorScheme})`, async ({ page, context }, info) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.emulateMedia({ colorScheme });
    const denied: string[] = [];
    await context.route('**/*', (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin === ORIGIN && request.method() === 'GET' && !url.pathname.startsWith('/api/')) {
        return route.continue();
      }
      denied.push(`${request.method()} ${url.origin}${url.pathname}`);
      return route.abort();
    });
    const calls: { method: string; params: Record<string, unknown> }[] = [];
    let rejectVerification: ((reason: unknown) => void) | undefined;
    await context.exposeBinding('mobileOtpFixture', async (_source, method: string, params: Record<string, unknown>) => {
      calls.push({ method, params });
      if (method === 'session.bootstrap') return { status: 'signedOut' };
      if (method === 'auth.requestOtp') return { cooldown_seconds: 0 };
      if (method === 'auth.verifyOtp') {
        return new Promise((_resolve, reject) => { rejectVerification = reject; });
      }
      throw new Error(`Unexpected mobile bridge call: ${method}`);
    });
    await context.addInitScript(() => {
      localStorage.setItem('i18nextLng', 'en');
      Object.defineProperty(window, 'avibeMobile', {
        value: {
          contract: 1,
          call: (method: string, params: Record<string, unknown>) => (
            window as unknown as {
              mobileOtpFixture: (method: string, params: Record<string, unknown>) => Promise<unknown>;
            }
          ).mobileOtpFixture(method, params),
        },
      });
    });

    // Load the real bundled-page entry and CSS; the only fake is its native bridge.
    await page.goto('/mobile.html');
    await page.getByRole('button', { name: 'Sign in / Create account', exact: true }).click();
    await page.locator('input[type=email]').fill('otp-fixture@example.invalid');
    await page.locator('input[type=email]').press('Enter');
    const input = page.locator('#mobile-login-code');
    await expect(input).toBeEnabled();
    await expect(input).toHaveAttribute('autocomplete', 'one-time-code');
    await expect(input).toHaveAttribute('inputmode', 'numeric');
    await expect(input).toHaveAttribute('maxlength', '6');

    // Typing starts verification on digit six; the retry inserts the whole value
    // in one edit, as paste/AutoFill do. Native iOS AutoFill UI is not simulated.
    for (const [index, code] of ['246810', '135790'].entries()) {
      if (index === 0) {
        await input.pressSequentially(code);
      } else {
        await input.fill('');
        await input.focus();
        await page.keyboard.insertText(code);
      }
      await expect(input).toBeDisabled();
      await expect(input).toHaveValue(code);
      await expect(input).toHaveCSS('opacity', '0');
      const cells = input.locator('..').locator('[aria-hidden="true"] > span');
      await expect(cells).toHaveText(code.split(''));
      for (const cell of await cells.all()) {
        await expect(cell).toBeVisible();
        await expect(cell).toHaveCSS('opacity', '1');
        await expect(cell).toHaveCSS('font-size', '24px');
      }
      await expect.poll(() => calls.filter((call) => call.method === 'auth.verifyOtp')).toEqual(
        ['246810', '135790'].slice(0, index + 1).map((value) => ({
          method: 'auth.verifyOtp',
          params: { email: 'otp-fixture@example.invalid', code: value },
        })),
      );
      await page.screenshot({ path: info.outputPath(`otp-pending-${index}-${colorScheme}.png`) });
      expect(rejectVerification).toBeDefined();
      rejectVerification!({ code: 'invalid_or_expired', message: 'Fixture code expired.' });
      await expect(input).toBeEnabled();
      await expect(input).toHaveCSS('opacity', '0');
      await expect(page.locator('#mobile-login-code-feedback [role=alert]')).toBeVisible();
    }
    expect(denied).toEqual([]);
    expect(calls.filter((call) => call.method === 'auth.requestOtp')).toEqual([
      { method: 'auth.requestOtp', params: { email: 'otp-fixture@example.invalid' } },
    ]);
  });
}
