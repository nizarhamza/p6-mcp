# MCP for Primavera P6 brand assets

The mark is a plug whose prongs are two Gantt bars and a milestone: a connector for schedules.

## Files

| Folder | File | Use |
|---|---|---|
| ico | p6mcp.ico | Windows exe, installer, window and taskbar icon (16, 24, 32, 48, 64, 128, 256) |
| png | p6mcp-icon-{16..1024}.png | App icon at fixed sizes (16 and 24 use the simplified drawing) |
| png | p6mcp-lockup.png / @2x | Logo with name and descriptor, on light backgrounds |
| png | p6mcp-lockup-on-dark.png / @2x | Same, for dark backgrounds (transparent PNG) |
| png | p6mcp-mark.png / p6mcp-mark-on-dark.png | Symbol only, 64 px tall (@2x = 128) |
| png | p6mcp-banner.png / @2x | GitHub README banner, 1280 x 320 |
| svg | p6mcp-icon.svg | Master app icon (rounded navy tile) |
| svg | p6mcp-icon-small.svg | Simplified icon for 24 px and below |
| svg | p6mcp-mark*.svg | Symbol: full color, on dark, black, white |
| svg | p6mcp-lockup*.svg | Lockups: light, on dark, black, white (text outlined, no font needed) |
| svg | p6mcp-banner.svg | README banner master |

## Colors

| Name | Hex | Use |
|---|---|---|
| Navy | #0A1A33 | Icon tile, body of the mark, "MCP" on light |
| Blue | #1F6FEB | Bars and "for Primavera P6" on light backgrounds |
| Light blue | #58A6FF | Bars and "for Primavera P6" on dark backgrounds |
| Slate | #4A5B73 | Descriptor text on light |
| Mist | #9FB3D1 | Descriptor text on dark |

## Type

IBM Plex Sans (open source, SIL OFL). "MCP" Bold, tracking -0.03 em; "for Primavera P6" Medium at half its size
on the same baseline, in the second color (the tag line form of Oracle's trademark guidelines: the Oracle marks
never form the name on their own and stay smaller than it). Descriptor in Regular.

The lockups and the banner are generated (text outlined, PNG renderings included) by
`python tools/make_brand.py <Bold.ttf> <Medium.ttf> <Regular.ttf>`. Versions 1.x were called P6 MCP: the file
names keep the `p6mcp-` prefix.

## Rules

- Keep clear space around the mark equal to the height of one bar on each side.
- Do not recolor the bars, stretch the mark, or add effects (shadows, gradients).
- Below 32 px use p6mcp-icon-small.svg.
- "Primavera" and "P6" are trademarks of Oracle. State in the README that the project is not affiliated with or endorsed by Oracle.
- Never show "for Primavera P6" larger than "MCP" or in Oracle red.
