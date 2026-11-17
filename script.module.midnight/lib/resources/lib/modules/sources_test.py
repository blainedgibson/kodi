#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Sources Architecture Test Module
Tests the refactored sources architecture that calls sources() directly.
"""

import json
from resources.lib.modules import control
from resources.lib.modules.crewruntime import c
from resources.lib.modules.sources import Sources


class SourcesArchitectureTester:
    """Test the refactored sources architecture with extensive logging."""

    def __init__(self):
        self.log_prefix = '[SourcesArchTest]'

    def log(self, message, level=0):
        """Log with our prefix."""
        c.log(f'{self.log_prefix} {message}', level)

    def run_episode_test(self):
        """
        Test episode scraping with the new architecture.
        Tests "The Wire" S01E01 - classic show with established content.
        """
        self.log('='*80)
        self.log('EPISODE ARCHITECTURE TEST - The Wire S01E01')
        self.log('='*80)

        # Test data - The Wire S01E01 "The Target" (2002)
        title = 'The Target'
        year = '2002'
        imdb = 'tt0306414'
        tvdb = '79126'  # IMPORTANT: Was missing, needed for watchseries1
        tmdb = '1438'
        season = '1'
        episode = '1'
        tvshowtitle = 'The Wire'
        premiered = '2002-06-02'

        self.log(f'Title: {tvshowtitle} S{season.zfill(2)}E{episode.zfill(2)} - "{title}"')
        self.log(f'IMDB: {imdb} | TVDB: {tvdb} | TMDB: {tmdb} | Year: {year}')
        self.log(f'Premiered: {premiered}')
        self.log(f'Note: Classic show (2002-2008), IMDB 9.3/10 - likely has content')
        self.log('')

        # Import and instantiate scrapers manually
        self.log('Instantiating scrapers...')
        import importlib
        source_dict = []

        scraper_modules = [
            'resources.lib.sources.free.anymovies',
            'resources.lib.sources.free.rapidmoviez',
            'resources.lib.sources.free.myvideolink',
            'resources.lib.sources.free.watchseries1',
            'resources.lib.sources.free.nuvio',
            'resources.lib.sources.free.sootio',
            'resources.lib.sources.free.webstreamer',
            'resources.lib.sources.en_tor.torrentio',
        ]

        for module_path in scraper_modules:
            try:
                # Import the module
                mod = importlib.import_module(module_path)

                # Call source() to instantiate
                if hasattr(mod, 'source') and callable(getattr(mod, 'source')):
                    scraper_obj = mod.source()
                    source_dict.append((module_path, scraper_obj, 0))
                    self.log(f'  (OK) {module_path.split(".")[-1]}')
                else:
                    self.log(f'  (X) {module_path.split(".")[-1]} - no source() method', 1)
            except Exception as e:
                self.log(f'  (X) {module_path.split(".")[-1]} - failed: {e}', 1)

        self.log(f'Successfully instantiated {len(source_dict)} scrapers')
        self.log('')

        try:
            # Enable detailed logging
            c.devmode = True

            self.log('Creating Sources instance...')
            s = Sources()

            self.log('Calling get_movie_episode_sources()...')
            self.log('  (Should use new _scraper_sources_wrapper() pathway)')
            self.log('')

            # Call the main sources method
            threads = []
            mains, labels = s.get_movie_episode_sources(
                title=title,
                year=year,
                imdb=imdb,
                tvdb=tvdb,  # CRITICAL: Was missing - needed for watchseries1 URL construction
                tmdb=tmdb,
                season=season,
                episode=episode,
                tvshowtitle=tvshowtitle,
                premiered=premiered,
                source_dict=source_dict,
                content='episode',
                threads=threads
            )

            # Wait for all threads to complete
            self.log('Waiting for scrapers to finish...')
            for thread in threads:
                thread.join(timeout=30)

            # Get actual sources from s.sources (not from return value which is thread tracking)
            actual_sources = s.sources

            self.log('-'*80)
            self.log('RESULTS SUMMARY')
            self.log('-'*80)
            self.log(f'Total sources found: {len(actual_sources)}')

            if actual_sources:
                # Group by provider
                by_provider = {}
                for source in actual_sources:
                    provider = source.get('provider', 'Unknown')
                    by_provider.setdefault(provider, []).append(source)

                self.log('')
                self.log('Breakdown by provider:')
                for provider, sources in sorted(by_provider.items()):
                    # Count qualities
                    qualities = {}
                    for src in sources:
                        qual = src.get('quality', 'Unknown')
                        qualities[qual] = qualities.get(qual, 0) + 1

                    self.log(f'  {provider}: {len(sources)} sources')
                    self.log(f'    Qualities: {qualities}')

                # Show first 5 sources as examples
                self.log('')
                self.log('Example sources (first 5):')
                for i, source in enumerate(actual_sources[:5], 1):
                    self.log(f'  Source {i}:')
                    self.log(f'    Provider: {source.get("provider", "Unknown")}')
                    self.log(f'    Quality: {source.get("quality", "Unknown")}')
                    self.log(f'    Source: {source.get("source", "Unknown")}')
                    self.log(f'    Language: {source.get("language", "Unknown")}')
                    if 'info' in source:
                        self.log(f'    Info: {source.get("info", [])}')
            else:
                self.log('  (X) No sources found')

            self.log('')
            self.log('-'*80)
            self.log('TEST COMPLETE')
            self.log('-'*80)
            self.log('Check log file for detailed [Scraper] entries')
            self.log('Look for _scraper_sources_wrapper() calls')
            self.log('='*80)

            # Show notification
            control.infoDialog(
                f'Sources Test Complete\n\n'
                f'Found {len(actual_sources)} sources from {len(by_provider)} providers\n\n'
                f'Check log for details',
                heading='Sources Architecture Test'
            )

            return True

        except Exception as e:
            self.log('='*80, 2)
            self.log('EXCEPTION OCCURRED', 2)
            self.log('='*80, 2)
            self.log(f'Error: {str(e)}', 2)

            import traceback
            self.log('Traceback:', 2)
            self.log(traceback.format_exc(), 2)
            self.log('='*80, 2)

            control.infoDialog(
                f'Test Failed!\n\nError: {str(e)}\n\nCheck log for details',
                heading='Sources Architecture Test'
            )

            return False

    def run_movie_test(self):
        """
        Test movie scraping with the new architecture.
        Tests "The Matrix" (1999) - well-known movie.
        """
        self.log('='*80)
        self.log('MOVIE ARCHITECTURE TEST - The Matrix (1999)')
        self.log('='*80)

        # Test data
        title = 'The Matrix'
        localtitle = 'The Matrix'
        year = '1999'
        imdb = 'tt0133093'
        tmdb = '603'

        self.log(f'Title: {title}')
        self.log(f'IMDB: {imdb} | TMDB: {tmdb} | Year: {year}')
        self.log('')

        # Import and instantiate scrapers manually
        self.log('Instantiating scrapers...')
        import importlib
        source_dict = []

        scraper_modules = [
            'resources.lib.sources.free.anymovies',
            'resources.lib.sources.free.rapidmoviez',
            'resources.lib.sources.free.myvideolink',
            'resources.lib.sources.free.watchseries1',
            'resources.lib.sources.free.nuvio',
            'resources.lib.sources.free.sootio',
            'resources.lib.sources.free.webstreamer',
            'resources.lib.sources.en_tor.torrentio',
        ]

        for module_path in scraper_modules:
            try:
                # Import the module
                mod = importlib.import_module(module_path)

                # Call source() to instantiate
                if hasattr(mod, 'source') and callable(getattr(mod, 'source')):
                    scraper_obj = mod.source()
                    source_dict.append((module_path, scraper_obj, 0))
                    self.log(f'  (OK) {module_path.split(".")[-1]}')
                else:
                    self.log(f'  (X) {module_path.split(".")[-1]} - no source() method', 1)
            except Exception as e:
                self.log(f'  (X) {module_path.split(".")[-1]} - failed: {e}', 1)

        self.log(f'Successfully instantiated {len(source_dict)} scrapers')
        self.log('')

        try:
            # Enable detailed logging
            c.devmode = True

            self.log('Creating Sources instance...')
            sources_instance = Sources()

            self.log('Calling get_movie_episode_sources()...')
            self.log('  (Should use new _scraper_sources_wrapper() pathway)')
            self.log('')

            # Call the main sources method
            threads = []
            mains, labels = sources_instance.get_movie_episode_sources(
                title=title,
                year=year,
                imdb=imdb,
                tmdb=tmdb,
                tvdb=None,  # Not needed for movies
                season=None,
                episode=None,
                tvshowtitle=title,
                premiered=None,
                source_dict=source_dict,
                content='movie',
                threads=threads
            )

            # Wait for all threads to complete
            self.log('Waiting for scrapers to finish...')
            for thread in threads:
                thread.join(timeout=30)

            # Get actual sources from sources_instance.sources (not from return value)
            actual_sources = sources_instance.sources

            self.log('-'*80)
            self.log('RESULTS SUMMARY')
            self.log('-'*80)
            self.log(f'Total sources found: {len(actual_sources)}')

            if actual_sources:
                # Group by provider
                by_provider = {}
                for source in actual_sources:
                    provider = source.get('provider', 'Unknown')
                    by_provider.setdefault(provider, []).append(source)

                self.log('')
                self.log('Breakdown by provider:')
                for provider, sources in sorted(by_provider.items()):
                    # Count qualities
                    qualities = {}
                    for src in sources:
                        qual = src.get('quality', 'Unknown')
                        qualities[qual] = qualities.get(qual, 0) + 1

                    self.log(f'  {provider}: {len(sources)} sources')
                    self.log(f'    Qualities: {qualities}')

                # Show first 5 sources as examples
                self.log('')
                self.log('Example sources (first 5):')
                for i, source in enumerate(actual_sources[:5], 1):
                    self.log(f'  Source {i}:')
                    self.log(f'    Provider: {source.get("provider", "Unknown")}')
                    self.log(f'    Quality: {source.get("quality", "Unknown")}')
                    self.log(f'    Source: {source.get("source", "Unknown")}')
                    self.log(f'    Language: {source.get("language", "Unknown")}')
            else:
                self.log('  (X) No sources found')

            self.log('')
            self.log('-'*80)
            self.log('TEST COMPLETE')
            self.log('-'*80)
            self.log('='*80)

            # Show notification
            control.infoDialog(
                f'Sources Test Complete\n\n'
                f'Found {len(actual_sources)} sources from {len(by_provider)} providers\n\n'
                f'Check log for details',
                heading='Sources Architecture Test'
            )

            return True

        except Exception as e:
            self.log('='*80, 2)
            self.log('EXCEPTION OCCURRED', 2)
            self.log('='*80, 2)
            self.log(f'Error: {str(e)}', 2)

            import traceback
            self.log('Traceback:', 2)
            self.log(traceback.format_exc(), 2)
            self.log('='*80, 2)

            control.infoDialog(
                f'Test Failed!\n\nError: {str(e)}\n\nCheck log for details',
                heading='Sources Architecture Test'
            )

            return False


def test_refactored_architecture_episode():
    """Entry point for episode test (called from router)."""
    tester = SourcesArchitectureTester()
    return tester.run_episode_test()


def test_refactored_architecture_movie():
    """Entry point for movie test (called from router)."""
    tester = SourcesArchitectureTester()
    return tester.run_movie_test()
