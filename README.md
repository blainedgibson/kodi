# Kodi Repo - UI Redesign Plan (ChatGPT "Midnight" Theme)

Source: ChatGPT conversation "Kodi UI Redesign Plan", Nov 17, 2026
https://chatgpt.com/c/6afca98b-8898-83e8-84ee-58752715367f

## The problem (verified in source)
- script.module.thecrew v3.2.29 is the real code (243 Python files). plugin.video.thecrew v3.2.2 is a thin launcher.
- Colour tags in the module: orchid x66 (dominant), plus ~20 others: red, orange, white, gold, grey, gray, cyan, yellow, lime, lightskyblue, lawngreen, green, skyblue, purple, limegreen, lightblue, forestgreen.
- Icons: single icon.png + fanart.jpg. Menus are text directory listings; the skin renders them.

## Recommended palette ("The Crew: Midnight")
- Background / Midnight Charcoal: #171C24
- Normal text / Soft White: #E5E9EF
- Secondary text / Slate: #8C98A8
- Accent / Muted Teal: #56B6B0
- Selected item / Deep Teal: #263B42
- Warnings / Warm Amber: #E5B86A
- Errors / Muted Red: #D97979
- Kill orchid and all scattered lime, lawngreen, cyan, skyblue, orange, gold variations.
- Usage: main menu section names = teal, normal listings = soft white, descriptions = slate, warnings = amber, errors = muted red.
- Note: Kodi [COLOR] tags colour text only, not the list-item background or focus rectangle. The skin controls that.

## Icons and artwork
- Addon icon: 512x512 PNG, solid background, no transparency.
- Fanart: 1920x1080 JPG, subtle, dark, atmospheric.
- Optional banner 1000x562, optional clearlogo transparent PNG.
- Per-section icons (Movies, TV Shows, Sports, Kids, Collections, Live Channels): one consistent style, consistent line thickness, rounded corners, white or pale slate artwork, teal only for small details, no gradients inside icons, no emoji as permanent menu art.
- Design source icons at 256x256, transparent PNGs. Assign per-section via xbmcgui.ListItem.setArt({'icon': ..., 'thumb': ...}).
- Keep one addon identity icon; do not replace icon.png six times.

## Implementation
- Create resources/lib/ui_theme.py with a COLORS dict (AARRGGBB, FF = fully opaque) and a color(text, name) helper:
  - header FF56B6B0, primary FFE5E9EF, secondary FF8C98A8, accent FF56B6B0, warning FFE5B86A, error FFD97979
- Import it wherever menus are generated; replace [COLOR orchid] etc. by role (header / primary / secondary / warning / error), not by blind find-and-replace.
- Watch for nested colour tags; clean those up rather than wrapping the whole label again.
- Do not change URL parameters, scraping logic, playback functions, or directory modes. Keep the UI refactor separate from functionality.
- Artwork layout: resources/media/movies.png, tvshows.png, sports.png, kids.png, collections.png, live.png.

## Skin and view modes
- The addon supplies items, labels, and artwork; the skin decides presentation. The addon cannot reliably force a layout across skins.
- Test on Estuary first (predictable, clean baseline). Also worth trying: Arctic Zephyr Reloaded, Aeon Nox: Silvo.
- View modes: Big List or Icons for the six main sections; Wide or Big Wide for movie/TV listings.
- Selected item: normal = soft white, focused = white text, focus background = subtle teal or dark slate (skin-level, not controllable via [COLOR] tags).

## Things to avoid
- Rainbow colour coding (a different colour per section, then more inside submenus).
- Mixing icon styles (emoji, photos, flat, outline).
- Excessive glow / neon outlines.
- Unnecessary decoration (brackets, stars, arrows, separators, emojis in labels).
- Tiny low-contrast text (design for TV distance).

## Implementation order (phases)
1. Colour cleanup: create ui_theme.py, centralize the palette, remove orchid and misc colours.
2. Main menu artwork: create six matching section icons, assign via ListItem.setArt.
3. Addon branding: replace icon.png and fanart.jpg with the new Midnight identity.
4. Skin testing: Estuary first, then Big List / Wide / other views.
5. Cleanup: check every submenu, search screen, error message, and playback listing for leftover colours.

## Optional later
- Configurable themes (Midnight, Arctic, Classic) stored in addon settings, loading the right colour dict. Not for the first pass.
