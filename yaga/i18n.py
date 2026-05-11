import locale
from dataclasses import dataclass


TRANSLATIONS = {
    "en": {
        # Navigation / gallery
        "Photos": "Photos",
        "Pictures": "Pictures",
        "Overview": "Overview",
        "Videos": "Videos",
        "Screenshots": "Screenshots",
        "Locations": "Locations",
        "Nextcloud": "Nextcloud",
        "No pictures found": "No pictures found",
        # Header actions
        "Settings": "Settings",
        "Refresh": "Refresh",
        "Sort": "Sort",
        "Edit": "Edit",
        "Info": "Info",
        "Rotate clockwise": "Rotate clockwise",
        "Back": "Back",
        "Fullscreen": "Fullscreen",
        "Exit fullscreen": "Exit fullscreen",
        # Sort modes
        "Newest first": "Newest first",
        "Oldest first": "Oldest first",
        "Name": "Name",
        "Folder": "Folder",
        "None": "None",
        "Date": "Date",
        "Ascending": "Ascending",
        "Descending": "Descending",
        # Editor — nav
        "Filter": "Filter",
        "Adjust": "Adjust",
        "Effects": "Effects",
        "Sticker": "Sticker",
        "Crop": "Crop",
        # Editor — adjust panel
        "Brightness": "Brightness",
        "Contrast": "Contrast",
        "Red": "Red",
        "Green": "Green",
        "Blue": "Blue",
        "Reset": "Reset",
        "Apply": "Apply",
        "Add": "Add",
        # Editor — sticker panel
        "Smileys": "Smileys",
        "Emotions": "Emotions",
        "Symbols": "Symbols",
        "Frame": "Frame",
        "Text": "Text",
        "No frame": "No frame",
        "Text input…": "Text input…",
        "Text color": "Text color",
        "Reset brush strokes": "Reset brush strokes",
        "Brush size": "Brush size",
        "Drag to reorder": "Drag to reorder",
        "Path of the Photos folder on your Nextcloud server.":
            "Path of the Photos folder on your Nextcloud server.",
        "Folders": "Folders",
        "Photos on Nextcloud": "Photos on Nextcloud",
        "Remove": "Remove",
        "Remove this folder?": "Remove this folder?",
        "It will disappear from the gallery navigation. The files on disk are not deleted.":
            "It will disappear from the gallery navigation. The files on disk are not deleted.",
        "Edit folder": "Edit folder",
        "The name appears as the entry label in the gallery navigation.":
            "The name appears as the entry label in the gallery navigation.",
        "Path": "Path",
        "Don't inherit": "Don't inherit",
        "Parent folders won't include this folder's content during scans.":
            "Parent folders won't include this folder's content during scans.",
        "Show": "Show",
        "Which media types appear when this folder is opened.":
            "Which media types appear when this folder is opened.",
        "Both": "Both",
        "Images only": "Images only",
        "Videos only": "Videos only",
        "Search": "Search",
        "Filename, date, month, EXIF…": "Filename, date, month, EXIF…",
        "Search: %d hits": "Search: %d hits",
        "%d hits": "%d hits",
        "No results": "No results",
        # Nextcloud setup / reconnect dialogs
        "Nextcloud active": "Nextcloud active",
        "Enables or disables all Nextcloud functions":
            "Enables or disables all Nextcloud functions",
        "Set up connection": "Set up connection",
        "Connect to your Nextcloud": "Connect to your Nextcloud",
        "Set up": "Set up",
        "How would you like to connect to your Nextcloud?\n\n"
        "You can find the app-password QR code in your Nextcloud under:\n"
        "Settings → Security → App passwords → \"Create new app password\".":
            "How would you like to connect to your Nextcloud?\n\n"
            "You can find the app-password QR code in your Nextcloud under:\n"
            "Settings → Security → App passwords → \"Create new app password\".",
        "Manually": "Manually",
        "Nextcloud connection disabled": "Nextcloud connection disabled",
        "Enable the connection now so the image can be loaded.":
            "Enable the connection now so the image can be loaded.",
        "This image is stored in your Nextcloud.\nThe connection is currently disabled.":
            "This image is stored in your Nextcloud.\nThe connection is currently disabled.",
        "Connect once": "Connect once",
        "Connect permanently": "Connect permanently",
        # Status counters
        "Deleted %d items": "Deleted %d items",
        "Deleted %d/%d items (%d failed)": "Deleted %d/%d items (%d failed)",
        "Moved %d items": "Moved %d items",
        "Moved %d items (%d failed)": "Moved %d items (%d failed)",
        # Item actions
        "Open": "Open",
        "Delete": "Delete",
        "Move": "Move",
        "Share": "Share",
        "Open externally": "Open externally",
        "Close": "Close",
        "Empty": "Empty",
        "Choose folder": "Choose folder",
        # Selection mode
        "Cancel selection": "Cancel selection",
        "Delete selected": "Delete selected",
        "Move selected": "Move selected",
        "Share selected": "Share selected",
        "selected": "selected",
        "Delete selection?": "Delete selection?",
        "Photos will be moved to trash.": "Photos will be moved to trash.",
        # Share dialog
        "Share image": "Share image",
        "Share %d images": "Share %d images",
        "Choose how to share:": "Choose how to share:",
        "Email": "Email",
        "Cannot share Nextcloud items directly — open them first to download.":
            "Cannot share Nextcloud items directly — open them first to download.",
        "%d Nextcloud item(s) skipped (not downloaded locally).":
            "%d Nextcloud item(s) skipped (not downloaded locally).",
        # Rotation
        "Save rotation?": "Save rotation?",
        "The image has been rotated. Save the change?": "The image has been rotated. Save the change?",
        "Discard": "Discard",
        "Save": "Save",
        "Undo": "Undo",
        "Redo": "Redo",
        # Viewer
        "Could not load file": "Could not load file",
        # Settings UI
        "Media folders": "Media folders",
        "Photos folder": "Photos folder",
        "Pictures folder": "Pictures folder",
        "Overview folder": "Overview folder",
        "Videos folder": "Videos folder",
        "Screenshots folder": "Screenshots folder",
        "Optional locations": "Optional locations",
        "Add location": "Add location",
        "Appearance": "Appearance",
        "Grid": "Grid",
        "Photos per row": "Photos per row",
        "Theme": "Theme",
        "System": "System",
        "Light": "Light",
        "Dark": "Dark",
        "Language": "Language",
        "Use system language": "Use system language",
        "English": "English",
        "German": "German",
        "Thumbnails": "Thumbnails",
        "Clear thumbnail cache": "Clear thumbnail cache",
        "Clear cache": "Clear cache",
        "Video": "Video",
        "External player command": "External player command",
        "Leave empty to use built-in playback": "Leave empty to use built-in playback",
        "Folder structure": "Folder structure",
        "All media": "All media",
        "No configured folder exists yet.": "No configured folder exists yet.",
        # Item status
        "Deleted": "Deleted",
        "Moved": "Moved",
        "Could not complete action": "Could not complete action",
        # Delete dialogs
        "Delete media?": "Delete media?",
        "Delete this item from the gallery?": "Delete this item from the gallery?",
        "Cancel": "Cancel",
        # Nextcloud settings
        "Credentials": "Credentials",
        "Server URL": "Server URL",
        "Username": "Username",
        "App password": "App password",
        "Scan QR code": "Scan QR code",
        "Photos folder on Nextcloud": "Photos folder on Nextcloud",
        "Create app password": "Create app password",
        "Nextcloud → Settings → Security → App passwords": "Nextcloud → Settings → Security → App passwords",
        "Performance": "Performance",
        "Load thumbnails only": "Load thumbnails only",
        "Skip downloading full files during sync": "Skip downloading full files during sync",
        "Show in Overview": "Show in Overview",
        "Merge Nextcloud items into the Overview (thumbnails load on demand)":
            "Merge Nextcloud items into the Overview (thumbnails load on demand)",
        "Connect": "Connect",
        "Disconnect": "Disconnect",
        "Connected ✓": "Connected ✓",
        "Disconnected": "Disconnected",
        "Connecting…": "Connecting…",
        "Connection failed": "Connection failed",
        "Please fill in all fields.": "Please fill in all fields.",
        "QR code scanned – credentials entered ✓": "QR code scanned – credentials entered ✓",
        "QR code scanned successfully ✓": "QR code scanned successfully ✓",
        "QR code scan error": "QR code scan error",
        # Editor
        "No frame": "No frame",
        "Text input…": "Text input…",
        "Add": "Add",
        "Crop": "Crop",
        "Apply": "Apply",
        "Reset": "Reset",
        "Brightness": "Brightness",
        "Contrast": "Contrast",
        "Red": "Red",
        "Green": "Green",
        "Blue": "Blue",
        "White": "White",
        "Black": "Black",
        "Yellow": "Yellow",
        "Could not save edited image": "Could not save edited image",
        # Frame themes
        "Christmas": "Christmas",
        "New Year": "New Year",
        "Easter": "Easter",
        "Wedding": "Wedding",
        "Birthday": "Birthday",
        "Spring": "Spring",
        "Summer": "Summer",
        "Winter": "Winter",
        # QR/camera
        "GStreamer camera support missing.\nInstall: apt install gstreamer1.0-plugins-bad\n                    python3-gst-1.0": "GStreamer camera support missing.\nInstall: apt install gstreamer1.0-plugins-bad\n                    python3-gst-1.0",
        # Info dialog
        "Dimensions": "Dimensions",
        "Modified": "Modified",
        "Size": "Size",
    },
    "de": {
        # Navigation / gallery
        "Photos": "Fotos",
        "Pictures": "Bilder",
        "Overview": "Übersicht",
        "Videos": "Videos",
        "Screenshots": "Screenshots",
        "Locations": "Orte",
        "Nextcloud": "Nextcloud",
        "No pictures found": "Keine Bilder gefunden",
        # Header actions
        "Settings": "Einstellungen",
        "Refresh": "Aktualisieren",
        "Sort": "Sortierung",
        "Edit": "Bearbeiten",
        "Info": "Info",
        "Rotate clockwise": "Im Uhrzeigersinn drehen",
        "Back": "Zurück",
        "Fullscreen": "Vollbild",
        "Exit fullscreen": "Vollbild verlassen",
        # Sort modes
        "Newest first": "Neueste zuerst",
        "Oldest first": "Älteste zuerst",
        "Name": "Name",
        "Folder": "Ordner",
        "None": "Keine",
        "Date": "Datum",
        "Ascending": "Aufsteigend",
        "Descending": "Absteigend",
        # Editor — nav
        "Filter": "Filter",
        "Adjust": "Anpassen",
        "Effects": "Effekte",
        "Sticker": "Sticker",
        "Crop": "Zuschneiden",
        # Editor — adjust panel
        "Brightness": "Helligkeit",
        "Contrast": "Kontrast",
        "Red": "Rot",
        "Green": "Grün",
        "Blue": "Blau",
        "Reset": "Zurücksetzen",
        "Apply": "Anwenden",
        "Add": "Hinzufügen",
        # Editor — sticker panel
        "Smileys": "Smileys",
        "Emotions": "Emotionen",
        "Symbols": "Symbole",
        "Frame": "Rahmen",
        "Text": "Text",
        "No frame": "Kein Rahmen",
        "Text input…": "Texteingabe…",
        "Text color": "Textfarbe",
        "Reset brush strokes": "Pinselstriche zurücksetzen",
        "Brush size": "Pinselgröße",
        "Drag to reorder": "Zum Sortieren ziehen",
        "Path of the Photos folder on your Nextcloud server.":
            "Pfad des Foto-Ordners auf deinem Nextcloud-Server.",
        "Folders": "Ordner",
        "Photos on Nextcloud": "Fotos auf Nextcloud",
        "Remove": "Entfernen",
        "Remove this folder?": "Diesen Ordner entfernen?",
        "It will disappear from the gallery navigation. The files on disk are not deleted.":
            "Er verschwindet aus der Navigation. Die Dateien auf der Festplatte werden nicht gelöscht.",
        "Edit folder": "Ordner bearbeiten",
        "The name appears as the entry label in the gallery navigation.":
            "Der Name erscheint als Beschriftung in der Galerie-Navigation.",
        "Path": "Pfad",
        "Don't inherit": "Nicht vererben",
        "Parent folders won't include this folder's content during scans.":
            "Übergeordnete Ordner berücksichtigen den Inhalt dieses Ordners "
            "beim Scan nicht.",
        "Show": "Anzeigen",
        "Which media types appear when this folder is opened.":
            "Welche Medientypen erscheinen, wenn dieser Ordner geöffnet wird.",
        "Both": "Beides",
        "Images only": "Nur Bilder",
        "Videos only": "Nur Videos",
        "Search": "Suchen",
        "Filename, date, month, EXIF…": "Dateiname, Datum, Monat, EXIF…",
        "Search: %d hits": "Suche: %d Treffer",
        "%d hits": "%d Treffer",
        "No results": "Keine Treffer",
        # Nextcloud setup / reconnect dialogs
        "Nextcloud active": "Nextcloud aktiv",
        "Enables or disables all Nextcloud functions":
            "Aktiviert oder deaktiviert alle Nextcloud-Funktionen",
        "Set up connection": "Verbindung einrichten",
        "Connect to your Nextcloud": "Mit deiner Nextcloud verbinden",
        "Set up": "Einrichten",
        "How would you like to connect to your Nextcloud?\n\n"
        "You can find the app-password QR code in your Nextcloud under:\n"
        "Settings → Security → App passwords → \"Create new app password\".":
            "Wie möchtest du deine Nextcloud verbinden?\n\n"
            "Den App-Passwort-QR-Code findest du in deiner Nextcloud unter:\n"
            "Einstellungen → Sicherheit → App-Passwörter → „Neues App-Passwort erstellen“.",
        "Manually": "Manuell",
        "Nextcloud connection disabled": "Nextcloud-Verbindung deaktiviert",
        "Enable the connection now so the image can be loaded.":
            "Aktiviere jetzt die Verbindung, damit das Bild geladen werden kann.",
        "This image is stored in your Nextcloud.\nThe connection is currently disabled.":
            "Das Bild liegt in deiner Nextcloud.\nDie Verbindung ist deaktiviert.",
        "Connect once": "Einmalig verbinden",
        "Connect permanently": "Dauerhaft verbinden",
        # Status counters
        "Deleted %d items": "%d Elemente gelöscht",
        "Deleted %d/%d items (%d failed)": "%d/%d Elemente gelöscht (%d fehlgeschlagen)",
        "Moved %d items": "%d Elemente verschoben",
        "Moved %d items (%d failed)": "%d Elemente verschoben (%d fehlgeschlagen)",
        # Item actions
        "Open": "Öffnen",
        "Delete": "Löschen",
        "Move": "Verschieben",
        "Share": "Teilen",
        "Open externally": "Extern öffnen",
        "Close": "Schließen",
        "Empty": "Leer",
        "Choose folder": "Ordner wählen",
        # Selection mode
        "Cancel selection": "Auswahl aufheben",
        "Delete selected": "Ausgewählte löschen",
        "Move selected": "Ausgewählte verschieben",
        "Share selected": "Ausgewählte teilen",
        "selected": "ausgewählt",
        "Delete selection?": "Auswahl löschen?",
        "Photos will be moved to trash.": "Fotos werden in den Papierkorb verschoben.",
        # Share dialog
        "Share image": "Bild teilen",
        "Share %d images": "%d Bilder teilen",
        "Choose how to share:": "Übertragungsweg wählen:",
        "Email": "E-Mail",
        "Cannot share Nextcloud items directly — open them first to download.":
            "Nextcloud-Elemente lassen sich nicht direkt teilen – bitte vorher öffnen, damit sie heruntergeladen werden.",
        "%d Nextcloud item(s) skipped (not downloaded locally).":
            "%d Nextcloud-Element(e) übersprungen (nicht lokal verfügbar).",
        # Rotation
        "Save rotation?": "Rotation speichern?",
        "The image has been rotated. Save the change?": "Das Bild wurde gedreht. Änderung speichern?",
        "Discard": "Verwerfen",
        "Save": "Speichern",
        "Undo": "Rückgängig",
        "Redo": "Wiederherstellen",
        # Viewer
        "Could not load file": "Datei konnte nicht geladen werden",
        # Settings UI
        "Media folders": "Medienordner",
        "Photos folder": "Fotoordner",
        "Pictures folder": "Bilderordner",
        "Overview folder": "Übersichtsordner",
        "Videos folder": "Videoordner",
        "Screenshots folder": "Screenshotordner",
        "Optional locations": "Optionale Orte",
        "Add location": "Ort hinzufügen",
        "Appearance": "Darstellung",
        "Grid": "Raster",
        "Photos per row": "Fotos pro Zeile",
        "Theme": "Theme",
        "System": "System",
        "Light": "Hell",
        "Dark": "Dunkel",
        "Language": "Sprache",
        "Use system language": "Systemsprache verwenden",
        "English": "Englisch",
        "German": "Deutsch",
        "Thumbnails": "Vorschaubilder",
        "Clear thumbnail cache": "Thumbnail-Cache löschen",
        "Clear cache": "Cache löschen",
        "Video": "Video",
        "External player command": "Externer Player-Befehl",
        "Leave empty to use built-in playback": "Leer lassen für integrierte Wiedergabe",
        "Folder structure": "Ordnerstruktur",
        "All media": "Alle Medien",
        "No configured folder exists yet.": "Noch kein konfigurierter Ordner vorhanden.",
        # Item status
        "Deleted": "Gelöscht",
        "Moved": "Verschoben",
        "Could not complete action": "Aktion konnte nicht ausgeführt werden",
        # Delete dialogs
        "Delete media?": "Medium löschen?",
        "Delete this item from the gallery?": "Dieses Element aus der Galerie löschen?",
        "Cancel": "Abbrechen",
        # Nextcloud settings
        "Credentials": "Zugangsdaten",
        "Server URL": "Server-URL",
        "Username": "Benutzername",
        "App password": "App-Passwort",
        "Scan QR code": "QR-Code scannen",
        "Photos folder on Nextcloud": "Foto-Ordner auf Nextcloud",
        "Create app password": "App-Passwort erstellen",
        "Nextcloud → Settings → Security → App passwords": "Nextcloud → Einstellungen → Sicherheit → App-Passwörter",
        "Performance": "Performance",
        "Load thumbnails only": "Nur Vorschaubilder laden",
        "Skip downloading full files during sync": "Beim Abgleich keine vollständigen Dateien herunterladen",
        "Show in Overview": "In Übersicht anzeigen",
        "Merge Nextcloud items into the Overview (thumbnails load on demand)":
            "Nextcloud-Inhalte in die Übersicht einblenden (Vorschaubilder werden bei Bedarf geladen)",
        "Connect": "Verbinden",
        "Disconnect": "Trennen",
        "Connected ✓": "Verbunden ✓",
        "Disconnected": "Getrennt",
        "Connecting…": "Verbinde…",
        "Connection failed": "Verbindung fehlgeschlagen",
        "Please fill in all fields.": "Bitte alle Felder ausfüllen.",
        "QR code scanned – credentials entered ✓": "QR-Code gescannt – Zugangsdaten eingetragen ✓",
        "QR code scanned successfully ✓": "QR-Code erfolgreich gescannt ✓",
        "QR code scan error": "QR-Code-Fehler",
        # Editor
        "No frame": "Ohne Rahmen",
        "Text input…": "Text eingeben…",
        "Add": "Hinzufügen",
        "Crop": "Zuschneiden",
        "Apply": "Übernehmen",
        "Reset": "Zurücksetzen",
        "Brightness": "Helligkeit",
        "Contrast": "Kontrast",
        "Red": "Rot",
        "Green": "Grün",
        "Blue": "Blau",
        "White": "Weiß",
        "Black": "Schwarz",
        "Yellow": "Gelb",
        "Could not save edited image": "Bearbeitetes Bild konnte nicht gespeichert werden",
        # Frame themes
        "Christmas": "Weihnachten",
        "New Year": "Silvester",
        "Easter": "Ostern",
        "Wedding": "Hochzeit",
        "Birthday": "Geburtstag",
        "Spring": "Frühling",
        "Summer": "Sommer",
        "Winter": "Winter",
        # QR/camera
        "GStreamer camera support missing.\nInstall: apt install gstreamer1.0-plugins-bad\n                    python3-gst-1.0": "GStreamer-Kamera-Unterstützung fehlt.\nInstallieren: apt install gstreamer1.0-plugins-bad\n              python3-gst-1.0",
        # Info dialog
        "Dimensions": "Abmessungen",
        "Modified": "Geändert",
        "Size": "Größe",
    },
}


def system_language() -> str:
    lang, _encoding = locale.getlocale()
    if lang and lang.lower().startswith("de"):
        return "de"
    return "en"


@dataclass
class Translator:
    language: str = "system"

    @property
    def active_language(self) -> str:
        if self.language == "system":
            return system_language()
        return self.language if self.language in TRANSLATIONS else "en"

    def gettext(self, text: str) -> str:
        return TRANSLATIONS.get(self.active_language, TRANSLATIONS["en"]).get(text, text)
