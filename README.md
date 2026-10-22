# VOXEL Launcher

A personal, non-commercial Minecraft: Java Edition launcher for Arch Linux (and derivatives:
EndeavourOS, Manjaro, CachyOS), written in Python with Tkinter.

Features: instances ("builds") with separate mod folders, Fabric/Vanilla, optional performance-mod
auto-install, per-build backgrounds, GameMode/MangoHud support, offline mode and Microsoft sign-in.

## Requirements

```
sudo pacman -S python tk python-requests pciutils xdg-utils polkit
yay -S python-minecraft-launcher-lib
```

Optional: `python-pillow` (backgrounds), `gamemode`, `mangohud`, `libnotify`, `libsecret`, `jre21-openjdk`.
Under Wayland compositors without built-in XWayland (e.g. niri) install and run `xwayland-satellite`.

## Run

```
python voxel-launcher.py            # GUI
python voxel-launcher.py --doctor   # environment diagnostics, no GUI
```

## Microsoft sign-in (how authentication works)

The launcher uses the standard OAuth 2.0 authorization code flow with PKCE as a public client
(no client secret) and a loopback redirect to `http://localhost:<random port>`:

1. The browser opens the Microsoft sign-in page; the launcher listens on a local port only until
   the redirect arrives (or the user cancels / the timeout passes).
2. The authorization code is exchanged for tokens, then Xbox Live -> XSTS -> Minecraft Services
   (`login_with_xbox`) via [minecraft-launcher-lib](https://pypi.org/project/minecraft-launcher-lib/).
3. The refresh token is stored locally only (Secret Service via `secret-tool` if available,
   otherwise a config file with mode 600). Nothing is sent to any server operated by this project.

Calling the Minecraft Services API requires an Azure application ID approved by Mojang.
You can use your own: set it in Settings -> "Azure Client ID". Redirect URIs to register under
"Mobile and desktop applications": `http://localhost` (and optionally
`https://login.live.com/oauth20_desktop.srf` for the manual fallback), with "Allow public client flows" enabled.
Without sign-in the launcher works in offline mode.

## Disclaimer

Not an official Minecraft product. Not approved by or associated with Mojang or Microsoft.
You need your own legitimate copy of Minecraft: Java Edition to play online.

## License

MIT, see [LICENSE](LICENSE).
