// Any other page (custom apps, shared canvases, whiteboards): just load it; it asks for the mic itself.
// If it has a visible share button, use it; otherwise z0's camera tile shows the share surface.
export default {
  name: 'generic',
  match: /./,
  async join() {},
  async share(page) {
    const b = page.getByRole('button', { name: /^(Share screen|Share your screen|Present( now)?|Screen ?share)$/i }).first();
    if (!(await b.waitFor({ state: "visible", timeout: 1500 }).then(() => true, () => false))) throw new Error('no share button');
    await b.click({ timeout: 5000 });
  },
};
