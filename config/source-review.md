# Morning Bar source list

The user approved this list on 2026-11-21, with Wuthering Waves and Neverness
to Everness removed. The active copy is
`~/.config/morningbar/interests.toml`; exact URLs, source types, trust values,
and per-source item caps are in
[`interests.proposal.toml`](interests.proposal.toml).

## Coverage

| Interest | Candidate sources |
|---|---|
| Nvidia and wider tech | NVIDIA Newsroom, NVIDIA Developer Blog, NVIDIA Blog, Ars Technica, TechCrunch AI, Hacker News top stories |
| AI research | arXiv efficiency search, Hugging Face Daily Papers JSON API, llama.cpp releases |
| Math and quantitative finance | arXiv q-fin, arXiv math.PR |
| Android and kernels | KernelSU-Next, SukiSU-Ultra, pershoot SuSFS, Tricky Store, Play Integrity Fork, ReZygisk, phh GSI, Android Play Integrity notes, Samsung Security Updates, Android Developers Blog, Linux stable releases |
| Linux and gaming compatibility | Arch news and security advisories, Hyprland, Niri, Quickshell, end-4, Mesa, WineHQ, GE-Proton, Heroic, Phoronix, LWN, GamingOnLinux, libfprint, python-validity |
| Games | Official Genshin and Zenless Zone Zero news pages; JShoe and Zy0x YouTube RSS |
| Anime | Anime News Network RSS and AniList airing API |
| Markets news | Federal Reserve and ECB news/calendars, BLS release calendar, FundedNext rule page |
| Study and scholarships | Cambridge news, key dates, timetables and four syllabus pages; Tsinghua, GKS, KAIST, HKU, MEXT, NAWA, KNB, Study in Romania, Erasmus Mundus |

## Validation notes

- Checked all 65 retained URLs on 2026-11-21. Feeds and APIs returned HTTP 200
  with RSS, Atom, JSON, XML, or iCalendar content; page watchers returned HTTP
  200 HTML. AniList was checked with a read-only public GraphQL query.
- The final HTML validation pass checked `robots.txt` first. The `SM-A165F`
  Samsung firmware page was excluded because Samsung's document host returned
  HTTP 403 for `robots.txt`; the general Samsung Security Updates page is
  included.
- Correction: an earlier status probe fetched the first response chunk from
  HTML pages before checking their robots policy, and the robots verifier
  fetched `robots.txt` twice per host. That was a mistake in my validation
  tooling, not the collector. I stopped further page probing after finding it.
  The runtime collector itself checks robots before each page request and uses
  one host limiter for requests.
- The CampusChina homepage returned HTTP 412 to the public client and was
  excluded. Tsinghua's official scholarship page is included; no login or bot
  check is bypassed.
- Cambridge's timetable page contains multiple administrative zones. The
  appropriate zone is not inferred from the local timezone and needs the
  user's confirmation.
- The approved JShoe feed points to the main [Jshoea channel](https://www.youtube.com/channel/UC7cOIpcyDamAuldTCb52TMw)
  (channel ID `UC7cOIpcyDamAuldTCb52TMw`). The alternate [Jshoea2 channel](https://www.youtube.com/channel/UCgI5HefCKaGOCnTgH-mExZw)
  is a clips/VOD channel and is not included. The Zy0x proposal points to
  channel ID `UCgo5pn2U8qbl_WYXU8Q6f0w`.
- University and Cambridge dates remain unverified until an official notice
  confirms them. The briefing will link directly to the official page and
  label scraped dates as unconfirmed.
- The game feed list follows the user's preference for Genshin Impact and
  Zenless Zone Zero; Wuthering Waves and Neverness to Everness are excluded.

## Policy and request behavior

RSS, Atom, JSON, and iCalendar are preferred. HTML watchers are used only for
official pages without a usable feed. The collector checks robots.txt, sends an
honest `MorningBar/0.1` user agent, rate limits each host, caps response sizes,
and uses ETag/Last-Modified when provided. NVIDIA RSS is provided for personal,
non-commercial use; the proposal is for this local personal reader only.
