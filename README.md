# Google Play Books - Android Notes exporter

## Why
This tool can be handy, if you run into sync issues of google play books and don't find an option to backup/export your annotations & highlights you made on your Google Play Books Android app (e.g. [see](https://www.reddit.com/r/GooglePlayBooks/comments/1xq08bf/noteshighlights_from_android_boox_go_6_not/) [these](https://support.google.com/googleplay/thread/453109335/notes-highlights-not-syncing-across-devices-in-google-play-books-page-position-sync-works-fine) [issues](https://support.google.com/googleplay/thread/383293083/play-books-highlight-sync-issue?hl=en))

## Prerequisits
- Android Device
   - with Google Play Books which has notes and/or highlights you want to export
   - Developer Mode activated on your Android device

- Computer
   - Python 3.10+ 
   - Android Platform Tools (adb)

- USB Cable to connect Android and computer



## How to use

1. Connect BOOX over USB, enable USB debugging and approve the computer on the device.
2. Open the desired book in Play Books, then go to the nots overview via the burger menue (which you can access in the "page scrollig" mode reachable via a single tap on any page)
3. Scroll to the very top yourself. Keep this screen open, and the orientation fixed throughout capture.
4. In PowerShell , run:

```powershell
python .\backup_playbooks.py --adb C:\adb\adb.exe --output E:\downloads\boox\backups
```

The script captures XML, immediately pulls it to the PC, then slowly swipes upward inside the Notes list. Coordinates come from the actual XML, not a fixed screen resolution. 

Each run creates a separate timestamped folder containing raw XML, readable annotations.md, Excel-friendly annotations.csv, annotations.json, all ungrouped observations.json, and run.json with the stop reason. 

It starts at the CURRENT scroll position, not automatically at the beginning. Three unchanged captures following swipes indicate no detected movement, not proof of completeness. Verify the first and last captured entries. Default maximum is 500 screens; reaching that limit is recorded in run.json.

The export preserves annotation text as provided by the Notes list.  This backup cannot restore annotations into Play Books. Exact identical rows are grouped for readability; all original observations remain available, including incomplete edge rows. Different annotations with identical text/fields can be grouped, and partial versions of a row may appear separately.

Options:

```powershell
python .\backup_playbooks.py --help
python .\backup_playbooks.py --delay 4 --max-screens 1000 --output E:\downloads\boox\backups
python .\backup_playbooks.py --serial DEVICE_SERIAL --output E:\downloads\boox\backups
```

Re-export existing XML offline (never connects to the device):

```powershell
python .\backup_playbooks.py --import-xml E:\downloads\boox --output E:\downloads\boox\backups
```

No automatic wake, settings changes, scrolling to the top, or navigation into individual notes is performed. If the list disappears or the book title changes, the script stops.
