// Jitsi Meet (meet.jit.si or any Jitsi server). On meet.jit.si the room's creator signs in first.
export default {
  name: 'jitsi',
  match: /jit\.si|jitsi/,
  async join(page, cfg) {
    const name = page.locator('input#premeeting-name-input, input[placeholder*="name" i]');
    if (await name.first().waitFor({ state: "visible", timeout: 15000 }).then(() => true, () => false)) await name.first().fill(cfg.name);
    await page.getByRole('button', { name: /Join meeting/ }).first().click({ timeout: 15000 }).catch(() => {});
  },
  async share(page) { await page.getByRole('button', { name: /Start screen sharing|Share your screen/ }).first().click({ timeout: 8000 }); },
};
