# -*- coding: utf-8 -*-

'''
 ***********************************************************
 * Midnight Add-on - Scraper Status Window
 *
 * @file scraper_status_window.py
 * @package script.module.midnight
 *
 * @copyright (c) 2026, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 ***********************************************************
'''

import xbmc
import xbmcgui
from datetime import datetime
import sys
import os
from resources.lib.ui_theme import color


# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from resources.lib.modules import control
from resources.lib.modules.scraper_test import (
    ScraperTester,
    is_internal_scraper,
    is_user_toggle_eligible,
)
from resources.lib.modules.scraper_registry import get_registry
from resources.lib.modules.crewruntime import c


class ScraperStatusWindow(xbmcgui.WindowXMLDialog):
    """Window to display scraper status and testing."""

    def __init__(self, *args, **kwargs):
        xbmcgui.WindowXMLDialog.__init__(self)
        self.tester = ScraperTester()
        self.current_category = 'direct'
        self.status_data = None
        self.testing_in_progress = False

    def onInit(self):
        """Initialize the window."""
        # Set modern fanart background
        try:
            fanart = c.addon_fanart()
            self.setProperty('fanart', fanart)
        except:
            pass

        # Load cached status or test if needed
        self.load_status()

        # Set default category to direct sources
        self.setProperty('category', 'direct')
        self._sync_category_radios(3001)
        xbmc.executebuiltin('Control.SetFocus(3001)')
        self.update_category_display('direct')

    def load_status(self):
        """Load scraper status from cache or run basic test."""
        c.log('[ScraperStatus] load_status() called')
        self.status_data = self.tester.load_status()
        c.log(f'[ScraperStatus] Loaded status_data from cache: {self.status_data is not None}')

        if self.status_data:
            c.log(f'[ScraperStatus] Cache has {len(self.status_data.get("direct", []))} direct scrapers')

        if not self.status_data or not self.tester.is_cache_valid(max_age_hours=24):
            c.log('[ScraperStatus] Cache invalid or empty, testing scrapers...')
            # No valid cache, show loading dialog
            progress = xbmcgui.DialogProgress()
            progress.create(f'{color("MIDNIGHT", 'header')}', 'Scraping test titles...')

            def progress_callback(current, total, name):
                percent = int((current / total) * 100)
                progress.update(percent, f'Testing {current}/{total}: {name}')
                if progress.iscanceled():
                    return False
                return True

            self.status_data = self.tester.test_all_scrapers(
                test_type='scrape',
                progress_callback=progress_callback
            )
            c.log(f'[ScraperStatus] After testing: {len(self.status_data.get("direct", []))} direct scrapers')
            progress.close()

        # Update summary display
        self.update_summary()

    def update_summary(self):
        """Update the summary information."""
        if not self.status_data:
            return

        summary = self.status_data.get('summary', {})

        summary_text = (
            f"Total: {summary.get('total', 0)} | "
            f"{color('Working: ' + str(summary.get('working', 0)), 'accent')} | "
            f"{color('Empty/Fail: ' + str(summary.get('accessible', 0)), 'warning')} | "
            f"{color('Blocked: ' + str(summary.get('blocked', 0)), 'error')} | "
            f"{color('Disabled: ' + str(summary.get('disabled', 0)), 'secondary')} | "
            f"{color('User Off: ' + str(summary.get('user_disabled', 0)), 'warning')}")

        # Format the timestamp
        last_updated_raw = summary.get('last_updated')
        if last_updated_raw:
            try:
                # Parse ISO format timestamp
                dt = datetime.fromisoformat(last_updated_raw)
                # Format as readable string
                last_updated = dt.strftime('%b %d, %Y %I:%M %p')
            except Exception:
                last_updated = 'Click "Test All Scrapers" to run tests'
        else:
            last_updated = 'Click "Test All Scrapers" to run tests'

        self.setProperty('summary', summary_text)
        self.setProperty('summary_text', summary_text)
        self.setProperty('last_updated', last_updated)

    _CATEGORY_CONTROLS = (
        (3001, 'direct'),
        (3002, 'torrent'),
        (3003, 'usenet'),
    )

    def _sync_category_radios(self, selected_id):
        """Keep one category tab selected (radiobuttons toggle independently in XML)."""
        for control_id, _ in self._CATEGORY_CONTROLS:
            try:
                self.getControl(control_id).setSelected(control_id == selected_id)
            except Exception:
                pass

    def _resolve_active_category(self, clicked_id):
        """Map tab click to category; unchecking a tab restores another selected tab."""
        category_by_id = dict(self._CATEGORY_CONTROLS)

        try:
            if self.getControl(clicked_id).isSelected():
                return category_by_id[clicked_id], clicked_id
        except Exception:
            return category_by_id.get(clicked_id, 'direct'), clicked_id

        for control_id, category in self._CATEGORY_CONTROLS:
            if control_id == clicked_id:
                continue
            try:
                if self.getControl(control_id).isSelected():
                    return category, control_id
            except Exception:
                pass

        return 'direct', 3001

    def _handle_category_click(self, control_id):
        """Switch list to clicked category, or restore a tab when one is unchecked."""
        category, active_id = self._resolve_active_category(control_id)
        self._sync_category_radios(active_id)
        self.setProperty('category', category)
        self.update_category_display(category)

    def update_category_display(self, category):
        """Update the scraper list for the selected category."""
        c.log(f'[ScraperStatus] update_category_display called for category: {category}')

        if not self.status_data:
            c.log('[ScraperStatus] No status_data available!')
            return

        self.current_category = category
        scrapers = self.status_data.get(category, [])
        c.log(f'[ScraperStatus] Found {len(scrapers)} scrapers for category {category}')

        # Clear the list
        self.clearList()

        # Populate the list
        for scraper in scrapers:
            c.log(f'[ScraperStatus] Adding scraper: {scraper.get("name")}')
            list_item = self.create_list_item(scraper)
            self.getControl(5000).addItem(list_item)

    def create_list_item(self, scraper):
        """Create a list item for a scraper."""
        name = scraper.get('name', 'Unknown')
        accessible = scraper.get('accessible', False)
        working = scraper.get('working', False)
        disabled = scraper.get('disabled', False)
        defunct = scraper.get('defunct', False)
        user_disabled = scraper.get('user_disabled', False)
        internal = scraper.get('internal', is_internal_scraper(scraper.get('module', '')))
        pack_capable = scraper.get('pack_capable', False)
        response_time = scraper.get('response_time')
        error = scraper.get('error')

        # Create list item
        list_item = xbmcgui.ListItem(label=name)

        # Set properties based on status
        if user_disabled:
            status_icon = 'DefaultAddonDisabled.png'
            status_text = f'{color("USER DISABLED", 'warning')}'
            status_color = 'FFFFCC00'
        elif disabled and defunct:
            # Defunct scrapers (site permanently closed)
            status_icon = 'DefaultAddonBroken.png'  # Red X
            status_text = f'{color("X Defunct (Site Closed)", 'secondary')}'
            if error:
                status_text += f' - {error[:50]}'
            status_color = 'FF606060'
        elif disabled:
            # Disabled scrapers - SHOW IN RED
            status_icon = 'DefaultAddonBroken.png'  # Red X
            status_text = f'{color("X DISABLED", 'error')}'
            status_color = 'FFFF0000'
        elif working:
            # Real scrape returned sources
            status_icon = 'DefaultAddonEnabled.png'
            status_text = f'{color("OK Working", 'accent')}'
            if error:
                status_text += f' - {error[:50]}'
            status_color = 'FF00FF00'
        elif accessible:
            # Reachable but empty / timeout / soft error (not CF-blocked)
            status_icon = 'DefaultAddonEnabled.png'
            detail = (error or 'no sources')[:50]
            if error and error.startswith('empty'):
                status_text = f'{color("Empty", 'warning')} - {detail}'
            elif error and error.startswith('timeout'):
                status_text = f'{color("Timeout", 'warning')} - {detail}'
            elif error and error.startswith('error'):
                status_text = f'{color("Error", 'error')} - {detail}'
            else:
                status_text = f'{color("OK Accessible", 'accent')} - {detail}'
            status_color = 'FFFFA500'
        else:
            # Reserved for clear CF / challenge blocks
            status_icon = 'DefaultAddonBroken.png'
            status_text = f'{color("X Blocked", 'error')}'
            if error:
                status_text += f' - {error[:40]}'
            status_color = 'FFFF0000'

        list_item.setProperty('status_icon', status_icon)
        list_item.setProperty('status', status_text)
        list_item.setProperty('status_color', status_color)
        list_item.setProperty('pack_capable', str(pack_capable).lower())

        if pack_capable:
            list_item.setProperty('pack_badge', '[PACK]')  # Show PACK text badge

        if response_time:
            response_text = f'{response_time:.2f}s'
            list_item.setProperty('response_time', response_text)
        else:
            response_text = 'N/A'

        # Store complete scraper data in the list item for details dialog
        list_item.setProperty('scraper_name', name)
        list_item.setProperty('scraper_module', scraper.get('module', ''))
        list_item.setProperty('scraper_category', scraper.get('category', ''))
        list_item.setProperty('scraper_accessible', str(accessible))
        list_item.setProperty('scraper_working', str(working))
        list_item.setProperty('scraper_disabled', str(disabled))
        list_item.setProperty('scraper_defunct', str(defunct))
        list_item.setProperty('scraper_user_disabled', str(user_disabled))
        list_item.setProperty('scraper_internal', str(internal))
        list_item.setProperty('scraper_pack_capable', str(pack_capable))
        list_item.setProperty('scraper_response_time', response_text)
        list_item.setProperty('scraper_error', error or 'None')
        if user_disabled:
            list_item.setProperty('user_badge', '[OFF]')

        return list_item

    def clearList(self):
        """Clear the scraper list."""
        try:
            self.getControl(5000).reset()
        except Exception:
            pass

    def _selected_scraper(self):
        try:
            return self.getControl(5000).getSelectedItem()
        except Exception:
            return None

    def toggle_selected_scraper(self):
        """Enable/disable the selected internal Midnight scraper."""
        selected_item = self._selected_scraper()
        if not selected_item:
            xbmcgui.Dialog().notification(
                f'{color("MIDNIGHT", 'header')}',
                'Select a scraper first',
                xbmcgui.NOTIFICATION_WARNING,
                2500,
            )
            return

        module = selected_item.getProperty('scraper_module')
        name = selected_item.getProperty('scraper_name')
        internal = selected_item.getProperty('scraper_internal') == 'True'

        if not internal and not is_user_toggle_eligible(module):
            xbmcgui.Dialog().notification(
                f'{color("MIDNIGHT", 'header')}',
                'This scraper cannot be toggled here',
                xbmcgui.NOTIFICATION_INFO,
                3500,
            )
            return

        user_disabled = selected_item.getProperty('scraper_user_disabled') == 'True'
        prompt = (
            f'Enable [B]{name}[/B] for source searches?'
            if user_disabled
            else f'Disable [B]{name}[/B] for source searches?'
        )

        if not xbmcgui.Dialog().yesno(
            f'{color("MIDNIGHT", 'header')} - {"Enable" if user_disabled else "Disable"} Scraper',
            prompt,
        ):
            return

        registry = get_registry()
        if user_disabled:
            ok = registry.set_user_disabled(module, False)
            msg = f'Enabled {name}' if ok else f'Could not enable {name}'
        else:
            ok = registry.set_user_disabled(module, True)
            msg = f'Disabled {name}' if ok else f'Could not disable {name}'

        xbmcgui.Dialog().notification(
            f'{color("MIDNIGHT", 'header')}',
            msg,
            xbmcgui.NOTIFICATION_INFO if ok else xbmcgui.NOTIFICATION_ERROR,
            3000,
        )

        if ok:
            self._sync_user_disabled_in_cache(module, not user_disabled)
            self.refresh_status()

    def _sync_user_disabled_in_cache(self, module_name, disabled):
        """Keep in-memory status_data aligned after a toggle."""
        if not self.status_data:
            return
        for bucket in ('direct', 'torrent', 'usenet'):
            for scraper in self.status_data.get(bucket, []):
                if scraper.get('module') == module_name:
                    scraper['user_disabled'] = bool(disabled)

    def show_scraper_item_menu(self):
        """Context menu for a selected scraper."""
        selected_item = self._selected_scraper()
        if not selected_item:
            return

        module = selected_item.getProperty('scraper_module')
        internal = (
            selected_item.getProperty('scraper_internal') == 'True'
            or is_user_toggle_eligible(module)
        )
        user_disabled = selected_item.getProperty('scraper_user_disabled') == 'True'

        options = []
        if internal:
            options.append('Enable Scraper' if user_disabled else 'Disable Scraper')
        options.append('View Details')

        choice = xbmcgui.Dialog().select(
            f'{color("MIDNIGHT", 'header')} - Scraper',
            options,
        )
        if choice < 0:
            return

        if internal and choice == 0:
            self.toggle_selected_scraper()
        elif (internal and choice == 1) or (not internal and choice == 0):
            self.show_scraper_details()

    def show_scraper_details(self):
        """Show detailed information about the selected scraper."""
        try:
            selected_item = self._selected_scraper()
            if not selected_item:
                return

            name = selected_item.getProperty('scraper_name')
            module = selected_item.getProperty('scraper_module')
            category = selected_item.getProperty('scraper_category')
            accessible = selected_item.getProperty('scraper_accessible') == 'True'
            working = selected_item.getProperty('scraper_working') == 'True'
            disabled = selected_item.getProperty('scraper_disabled') == 'True'
            defunct = selected_item.getProperty('scraper_defunct') == 'True'
            user_disabled = selected_item.getProperty('scraper_user_disabled') == 'True'
            internal = selected_item.getProperty('scraper_internal') == 'True'
            pack_capable = selected_item.getProperty('scraper_pack_capable') == 'True'
            response_time = selected_item.getProperty('scraper_response_time')
            error = selected_item.getProperty('scraper_error')

            # Build status summary
            if user_disabled:
                status = f'{color("User Disabled", 'warning')}'
            elif disabled and defunct:
                status = f'{color("Defunct (Site Closed)", 'secondary')}'
            elif disabled:
                status = f'{color("Disabled", 'secondary')}'
            elif working:
                status = f'{color("Working", 'accent')}'
            elif accessible:
                if error and error.startswith('empty'):
                    status = f'{color("Empty", 'warning')}'
                elif error and error.startswith('timeout'):
                    status = f'{color("Timeout", 'warning')}'
                elif error and error.startswith('error'):
                    status = f'{color("Error", 'error')}'
                else:
                    status = f'{color("Accessible", 'accent')}'
            else:
                status = f'{color("Blocked", 'error')}'

            # Build category name
            category_names = {
                'en': 'Direct (English)',
                'en_de': 'Direct (English/German)',
                'en_tor': 'Torrent',
                'direct': 'Direct',
                'torrent': 'Torrent',
                'usenet': 'Usenet',
            }
            category_text = category_names.get(category, category)

            # Build message
            message = (
                f'[B]Name:[/B] {name}\n'
                f'[B]Category:[/B] {category_text}\n'
                f'[B]Module:[/B] {module}\n'
                f'[B]Type:[/B] {"Internal Midnight" if is_internal_scraper(module) else "External Pack"}\n'
                f'[B]Status:[/B] {status}\n'
                f'[B]User Toggle:[/B] {"Off" if user_disabled else "On"}\n'
                f'[B]Response Time:[/B] {response_time}\n'
                f'[B]Pack Capable:[/B] {"Yes" if pack_capable else "No"}'
            )

            if error and error != 'None':
                label = 'Result' if (
                    error.startswith('ok')
                    or error.startswith('empty')
                    or error.startswith('timeout')
                    or error.startswith('error')
                    or error.startswith('blocked')
                ) else 'Error'
                message += f'\n[B]{label}:[/B] {error}'

            if internal:
                message += '\n\n[B]Tip:[/B] Use Enable/Disable to control this scraper in searches.'

            xbmcgui.Dialog().textviewer(
                f'{color("MIDNIGHT", 'header')} - Scraper Details',
                message
            )

        except Exception as e:
            c.log(f'[ScraperStatus] Error showing details: {e}')

    def onClick(self, controlID):
        """Handle button clicks."""
        if controlID == 5000:  # Scraper list item clicked
            self.show_scraper_item_menu()

        elif controlID in (3001, 3002, 3003):
            self._handle_category_click(controlID)

        elif controlID == 8001:  # Refresh status
            self.refresh_status()

        elif controlID == 8002:  # Test all scrapers
            self.test_all_scrapers()

        elif controlID == 8004:  # Enable / disable internal scraper
            self.toggle_selected_scraper()

        elif controlID == 8005:  # Auto-disable blocked internal scrapers
            self.auto_disable_blocked_scrapers()

        elif controlID == 8003:  # Close
            self.close()

    def auto_disable_blocked_scrapers(self):
        """Disable all blocked internal scrapers that are not already user-disabled."""
        if not self.status_data:
            self.load_status()

        candidates = list(self.tester.iter_auto_disable_candidates(self.status_data))
        if not candidates:
            xbmcgui.Dialog().notification(
                f'{color("MIDNIGHT", 'header')}',
                'No blocked or non-working scrapers to disable',
                xbmcgui.NOTIFICATION_INFO,
                3500,
            )
            return

        if not self.tester.is_cache_valid(max_age_hours=24):
            if not xbmcgui.Dialog().yesno(
                f'{color("MIDNIGHT", 'header')} - Auto Disable Blocked',
                'Scraper test results are older than 24 hours.\n'
                'Run "Test All Scrapers" first for accurate results.\n\n'
                'Continue anyway?',
            ):
                return

        names_preview = ', '.join(s.get('name', '?') for s in candidates[:8])
        if len(candidates) > 8:
            names_preview += f', +{len(candidates) - 8} more'

        if not xbmcgui.Dialog().yesno(
            f'{color("MIDNIGHT", 'header')} - Auto Disable Blocked',
            f'Disable [B]{len(candidates)}[/B] blocked or non-working scraper(s)?\n\n'
            f'{names_preview}\n\n'
            'Includes Midnight, CocoScrapers, Gears, and Viper scrapers.\n'
            'They will be skipped during source searches until you re-enable them here.',
        ):
            return

        count = get_registry().auto_disable_blocked(self.status_data)
        xbmcgui.Dialog().notification(
            f'{color("MIDNIGHT", 'header')}',
            f'Disabled {count} blocked scraper(s)',
            xbmcgui.NOTIFICATION_INFO if count else xbmcgui.NOTIFICATION_WARNING,
            4000,
        )
        if count:
            self.refresh_status()

    def refresh_status(self):
        """Reload status from cache."""
        self.load_status()
        self.update_category_display(self.current_category)

    def test_all_scrapers(self):
        """Run full test on all scrapers."""
        if self.testing_in_progress:
            xbmcgui.Dialog().notification(
                f'{color("MIDNIGHT", 'header')}',
                'Test already in progress',
                xbmcgui.NOTIFICATION_WARNING,
                3000
            )
            return

        # Confirm with user
        if not xbmcgui.Dialog().yesno(
            f'{color("MIDNIGHT", 'header')} - Test All Scrapers',
            'This will scrape each provider with a real title '
            '(Inception / Breaking Bad S01E01).\n'
            'This may take several minutes.\n\nContinue?'
        ):
            return

        self.testing_in_progress = True

        # Show progress dialog
        progress = xbmcgui.DialogProgress()
        progress.create(f'{color("MIDNIGHT", 'header')}', 'Scraping test titles...')

        def progress_callback(current, total, name):
            percent = int((current / total) * 100)
            progress.update(percent, f'Testing {current}/{total}: {name}')
            if progress.iscanceled():
                return False
            return True

        try:
            self.status_data = self.tester.test_all_scrapers(
                test_type='scrape',
                progress_callback=progress_callback
            )

            progress.close()

            # Update display
            self.update_summary()
            self.update_category_display(self.current_category)

            xbmcgui.Dialog().notification(
                f'{color("MIDNIGHT", 'header')}',
                'Scraper testing complete!',
                xbmcgui.NOTIFICATION_INFO,
                3000
            )

        except Exception as e:
            c.log(f'[ScraperStatus] Test error: {e}')
            progress.close()
            xbmcgui.Dialog().notification(
                f'{color("MIDNIGHT", 'header')}',
                f'Test failed: {str(e)}',
                xbmcgui.NOTIFICATION_ERROR,
                5000
            )

        finally:
            self.testing_in_progress = False

    def onAction(self, action):
        """Handle navigation actions."""
        action_id = action.getId()
        if action_id in (9, 10, 92, 216, 247, 257, 275, 61467, 61448):
            # Close window on back/escape
            self.close()
        elif action_id in (117,):  # Context menu
            self.show_scraper_item_menu()


def open_scraper_status():
    """Open the scraper status window."""
    try:
        addon_path, skin = c.resolve_dialog_skin('ScraperStatus.xml')
        window = ScraperStatusWindow('ScraperStatus.xml', addon_path, skin, '1080i')
        window.doModal()
        del window
    except Exception as e:
        c.log(f'[ScraperStatus] Error opening window: {e}')
        xbmcgui.Dialog().notification(
            f'{color("MIDNIGHT", 'header')}',
            f'Error opening scraper maintenance: {str(e)}',
            xbmcgui.NOTIFICATION_ERROR,
            5000
        )
