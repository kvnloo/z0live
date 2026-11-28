// Google Meet: guest join screen (the owner admits "z0" from inside the meeting).
export default {
  name: 'google-meet',
  match: /meet\.google\.com/,
  async join(page, cfg) {
    // Signed-out guests first get "Continue as guest" (Meet, Nov 2026); older builds show the name box directly.
    const guest = page.getByRole('button', { name: /Continue as guest/ }).or(page.getByText('Continue as guest', { exact: true })).first();
    if (await guest.waitFor({ state: 'visible', timeout: 12000 }).then(() => true, () => false)) await guest.click();
    // "Your name" (older) or "Enter your name" (Nov 2026); "Ask to join" stays disabled until it is filled.
    const name = page.locator('input[aria-label$="our name"], input[placeholder$="our name"]');
    if (await name.first().waitFor({ state: "visible", timeout: 15000 }).then(() => true, () => false)) await name.first().fill(cfg.name);
    // Signed in (Z0LIVE_PASSTHRU_STATE) as an account already in the call, Meet offers "Join here too" and
    // "Switch here"; never pick "Switch here", it moves that account's other session out of the call.
    // "Join here too" sits behind "Other ways to join" (Nov 2026).
    const join = page.getByRole('button', { name: /^(Ask to join|Join now|Join)$|Join here too$/ }).first();
    const other = page.getByRole('button', { name: /^Other ways to join$/ }).first();
    await join.or(other).first().waitFor({ state: 'visible', timeout: 20000 });
    if (!await join.isVisible() && await other.isVisible()) {
      // The "Switch the call here" tip covers the button until dismissed.
      await page.getByRole('button', { name: /^Got it$/ }).first().click({ timeout: 2000 }).catch(() => {});
      await other.click(); await page.waitForTimeout(600);
    }
    await join.click({ timeout: 10000 });
    // Anonymous joins from automated browsers are refused at the knock (Meet BotGuard, Nov 2026).
    await page.waitForTimeout(2500);
    if (await page.getByText("You can't join this video call").first().isVisible().catch(() => false))
      throw new Error("Meet refused the join (You can't join this video call); anonymous automated guests are blocked, set Z0LIVE_PASSTHRU_STATE to a signed-in Google storageState");
    // "Join here too" (and some admits) land with the mic muted; z0 must be heard.
    await page.getByRole('button', { name: /^Turn on microphone/ }).first().click({ timeout: 3000 }).catch(() => {});
  },
  async share(page) { await page.getByRole('button', { name: /Share screen|Present now/ }).first().click({ timeout: 8000 }); },
  async unshare(page) { await page.getByRole('button', { name: /Stop (sharing|presenting)/ }).first().click({ timeout: 8000 }).catch(() => {}); },
  async leave(page) { await page.getByRole('button', { name: /Leave call/ }).first().click({ timeout: 5000 }).catch(() => {}); },
};
