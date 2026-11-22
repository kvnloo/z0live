# passthru recipes

One file per meeting service. z0's meeting leg (`../meeting_leg.mjs`) loads every `*.mjs` here, plus any
in `$Z0LIVE_PASSTHRU_RECIPES` (colon-separated dirs, e.g. your own outside the repo), and uses the first
whose `match` fits the link. `generic.mjs` is the fallback and always goes last.

Recipes drive each service's **own, unmodified web app**: no forks, no servers of ours. Audio, video and
screen share are handled once for every service by `../inject.js`, so a recipe is only the clicks:

```js
export default {
  name: 'my-app',
  match: /my-app\.example\.com\//,       // which links this recipe handles
  home: 'https://my-app.example.com/',      // needed with create()
  rewrite: url => url,                      // optional: e.g. force the web client
  async create(page, cfg) { return link },  // optional: start a new meeting, return its share link
  async join(page, cfg) {},                 // fill cfg.name, press join
  async share(page) {},                     // optional: press the app's own share button
  async unshare(page) {},                   // optional
  async leave(page) {},                     // optional
};
```

When an upstream app moves a button, the fix is a few lines here. Prefer `getByRole`/`getByLabel` over CSS.
