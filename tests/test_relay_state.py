import json
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from bleo.relay_state import (
    HealthBook,
    RelayEntry,
    RelayPool,
    cooldown_for,
    default_path,
    fnv1a,
    ordered_candidates,
    validate_relay_url,
)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def entry(label, host=None):
    return RelayEntry(f'https://{host or label + ".example.com"}', label, 'secret-' + label, '')


class PoolCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = directory.name
        self.path = os.path.join(self.dir, 'relays.json')
        self.clock = Clock()
        self.health = HealthBook(clock=self.clock)

    def pool(self, *labels, mode='auto', sticky=None, entries=None):
        entries = entries or [entry(label) for label in labels]
        return RelayPool(self.path, entries=entries, mode=mode, sticky_url=sticky)

    def read(self):
        with open(self.path) as handle:
            return handle.read()

    def order(self, pool, **kwargs):
        return [e.label for e in ordered_candidates(pool, self.health, **kwargs)]


class OrderTests(PoolCase):
    def test_starts_at_sticky_and_rotates(self):
        pool = self.pool('a', 'b', 'c', 'd', sticky='https://c.example.com')
        self.assertEqual(self.order(pool), ['c', 'd', 'a', 'b'])

    def test_unknown_sticky_starts_at_first(self):
        pool = self.pool('a', 'b', sticky='https://gone.example.com')
        self.assertEqual(self.order(pool), ['a', 'b'])

    def test_healthy_first_then_cooling(self):
        pool = self.pool('a', 'b', 'c')
        self.health.mark_failure('https://a.example.com', 503)
        self.assertEqual(self.order(pool), ['b', 'c', 'a'])

    def test_cooling_relay_returns_when_cooldown_ends(self):
        pool = self.pool('a', 'b')
        self.health.mark_failure('https://a.example.com', 503)
        self.clock.now += 46
        self.assertEqual(self.order(pool), ['a', 'b'])

    def test_vercel_last_within_each_group(self):
        pool = self.pool(entries=[entry('v1', 'one.vercel.app'), entry('a'), entry('v2', 'two.vercel.app'),
                                  entry('b'), entry('c')])
        self.health.mark_failure('https://c.example.com', 503)
        self.health.mark_failure('https://two.vercel.app', 503)
        self.assertEqual(self.order(pool), ['a', 'b', 'v1', 'c', 'v2'])

    def test_mode_off_gives_nothing(self):
        self.assertEqual(self.order(self.pool('a', 'b', mode='off')), [])

    def test_empty_pool_gives_nothing(self):
        self.assertEqual(self.order(self.pool()), [])

    def test_sticky_is_an_alias_for_auto(self):
        self.assertEqual(self.pool('a', mode='sticky').mode, 'auto')
        with self.assertRaises(ValueError):
            self.pool('a', mode='round-robin')


class SpreadTests(PoolCase):
    def test_key_picks_a_stable_start(self):
        pool = self.pool('a', 'b', 'c', mode='spread')
        first = self.order(pool, spread_key='conversation-1')
        self.assertEqual(first, self.order(pool, spread_key='conversation-1'))
        shift = fnv1a('conversation-1') % 3
        self.assertEqual(first, ['a', 'b', 'c'][shift:] + ['a', 'b', 'c'][:shift])

    def test_fnv1a_known_values(self):
        self.assertEqual(fnv1a(''), 0x811C9DC5)
        self.assertEqual(fnv1a('a'), 0xE40C292C)
        self.assertEqual(fnv1a('foobar'), 0xBF9CF968)

    def test_cursor_rotates_without_a_key(self):
        pool = self.pool('a', 'b', 'c', mode='spread')
        self.assertEqual([self.order(pool)[0] for _ in range(4)], ['a', 'b', 'c', 'a'])

    def test_cooling_relays_stay_at_the_tail(self):
        pool = self.pool('a', 'b', 'c', mode='spread')
        self.health.mark_failure('https://a.example.com', 429)
        for key in ('k1', 'k2', 'k3', 'k4'):
            self.assertEqual(self.order(pool, spread_key=key)[-1], 'a')

    def test_auto_mode_ignores_the_key(self):
        pool = self.pool('a', 'b', 'c')
        self.assertEqual(self.order(pool, spread_key='k1'), ['a', 'b', 'c'])


class CooldownTests(PoolCase):
    url = 'https://a.example.com'

    def cooldown(self, status):
        book = HealthBook(clock=self.clock)
        book.mark_failure(self.url, status)
        return book.get(self.url).cooldown_until - self.clock.now

    def test_default_is_30_seconds(self):
        for status in (None, 0, 404, 408, 403, 401, 302):
            self.assertEqual(self.cooldown(status), 30, status)

    def test_429_is_90_seconds(self):
        self.assertEqual(self.cooldown(429), 90)

    def test_504_is_60_seconds(self):
        self.assertEqual(self.cooldown(504), 60)

    def test_other_5xx_is_45_seconds(self):
        for status in (500, 501, 502, 503, 520, 530):
            self.assertEqual(self.cooldown(status), 45, status)

    def test_402_is_six_hours(self):
        self.assertEqual(self.cooldown(402), 6 * 3600)

    def test_cooldown_for_matches(self):
        self.assertEqual([cooldown_for(s) for s in (429, 504, 500, 402, None)], [90, 60, 45, 21600, 30])

    def test_escalates_by_consecutive_failures_up_to_4x(self):
        seen = []
        for _ in range(6):
            self.health.mark_failure(self.url, 408)
            seen.append(self.health.get(self.url).cooldown_until - self.clock.now)
        self.assertEqual(seen, [30, 60, 90, 120, 120, 120])

    def test_escalation_cap_applies_to_the_status_base(self):
        for _ in range(5):
            self.health.mark_failure(self.url, 429)
        self.assertEqual(self.health.get(self.url).cooldown_until - self.clock.now, 360)

    def test_healthy_again_exactly_at_cooldown_end(self):
        self.health.mark_failure(self.url, 408)
        self.assertFalse(self.health.is_healthy(self.url))
        self.clock.now += 29
        self.assertFalse(self.health.is_healthy(self.url))
        self.clock.now += 1
        self.assertTrue(self.health.is_healthy(self.url))

    def test_success_resets_failures_and_cooldown(self):
        for _ in range(3):
            self.health.mark_failure(self.url, 429)
        self.health.mark_success(self.url, 0.2)
        record = self.health.get(self.url)
        self.assertEqual((record.consecutive_failures, record.cooldown_until, record.recovering), (0, 0.0, False))
        self.assertTrue(self.health.is_healthy(self.url))
        self.health.mark_failure(self.url, 408)
        self.assertEqual(self.health.get(self.url).cooldown_until - self.clock.now, 30)

    def test_success_without_latency_drops_the_record(self):
        self.health.mark_failure(self.url, 503)
        self.health.mark_success(self.url)
        self.assertIsNone(self.health.get(self.url))

    def test_get_returns_a_copy(self):
        self.health.mark_failure(self.url, 503)
        self.health.get(self.url).consecutive_failures = 99
        self.assertEqual(self.health.get(self.url).consecutive_failures, 1)


class EwmaTests(PoolCase):
    def feed(self, label, *samples):
        for sample in samples:
            self.health.mark_success(f'https://{label}.example.com', sample)

    def test_ewma_uses_alpha_0_3(self):
        self.feed('a', 1.0, 2.0)
        record = self.health.get('https://a.example.com')
        self.assertAlmostEqual(record.ewma, 0.3 * 2.0 + 0.7 * 1.0)
        self.assertEqual(record.ewma_samples, 2)

    def test_faster_measured_challenger_is_promoted(self):
        pool = self.pool('a', 'b', 'c')
        self.feed('a', 10, 10, 10)
        self.feed('b', 6, 6, 6)
        self.feed('c', 5, 5, 5)
        self.assertEqual(self.order(pool), ['c', 'a', 'b'])

    def test_margin_is_the_larger_of_1_5s_and_a_quarter(self):
        pool = self.pool('a', 'b')
        self.feed('a', 4, 4, 4)
        self.feed('b', 2.6, 2.6, 2.6)  # 1.4 s faster: under 1.5 s
        self.assertEqual(self.order(pool), ['a', 'b'])
        self.health.reset()
        self.feed('a', 10, 10, 10)
        self.feed('b', 7.6, 7.6, 7.6)  # 2.4 s faster but under 25% of 10 s
        self.assertEqual(self.order(pool), ['a', 'b'])
        self.health.reset()
        self.feed('a', 10, 10, 10)
        self.feed('b', 7.4, 7.4, 7.4)  # 2.6 s faster, over both
        self.assertEqual(self.order(pool), ['b', 'a'])

    def test_challenger_needs_three_samples(self):
        pool = self.pool('a', 'b')
        self.feed('a', 10, 10, 10)
        self.feed('b', 1, 1)
        self.assertEqual(self.order(pool), ['a', 'b'])
        self.feed('b', 1)
        self.assertEqual(self.order(pool), ['b', 'a'])

    def test_recovering_challenger_is_not_promoted(self):
        pool = self.pool('a', 'b')
        self.feed('a', 10, 10, 10)
        self.feed('b', 1, 1, 1)
        self.health.mark_failure('https://b.example.com', 503)
        self.clock.now += 31
        self.assertEqual(self.order(pool), ['a', 'b'])
        self.feed('b', 1)
        self.assertEqual(self.order(pool), ['b', 'a'])

    def test_head_without_ewma_keeps_the_lead(self):
        pool = self.pool('a', 'b')
        self.feed('b', 1, 1, 1)
        self.assertEqual(self.order(pool), ['a', 'b'])

    def test_challenger_applies_only_to_healthy_non_vercel(self):
        pool = self.pool(entries=[entry('a'), entry('v', 'fast.vercel.app'), entry('c')])
        self.feed('a', 10, 10, 10)
        self.health.mark_success('https://fast.vercel.app', 1)
        self.health.mark_success('https://fast.vercel.app', 1)
        self.health.mark_success('https://fast.vercel.app', 1)
        self.feed('c', 9, 9, 9)
        self.assertEqual(self.order(pool), ['a', 'c', 'v'])
        self.health.mark_failure('https://c.example.com', 503)
        self.assertEqual(self.order(pool), ['a', 'v', 'c'])


class PersistenceTests(PoolCase):
    def test_round_trip(self):
        pool = RelayPool(self.path)
        pool.add('https://a.example.com', auth='s1', label='alpha')
        pool.add('https://b.example.com', auth='s2')
        pool.set_sticky('https://b.example.com')
        pool.set_mode('spread')
        loaded = RelayPool.load(self.path)
        self.assertEqual([(e.url, e.label, e.auth) for e in loaded.entries],
                         [('https://a.example.com', 'alpha', 's1'), ('https://b.example.com', 'b', 's2')])
        self.assertEqual((loaded.sticky_url, loaded.mode), ('https://b.example.com', 'spread'))
        self.assertTrue(loaded.entries[0].added_at.endswith('Z'))

    def test_file_is_0600_and_so_is_its_directory_when_created(self):
        nested = os.path.join(self.dir, 'home', 'relays.json')
        RelayPool(nested).add('https://a.example.com', auth='s')
        self.assertEqual(stat.S_IMODE(os.stat(nested).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(nested)).st_mode), 0o700)

    def test_mode_stays_0600_when_overwriting_a_loose_file(self):
        pool = RelayPool(self.path)
        pool.add('https://a.example.com', auth='s')
        os.chmod(self.path, 0o644)
        pool.add('https://b.example.com', auth='s')
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_save_is_a_temp_file_in_the_same_directory_then_replace(self):
        pool = RelayPool(self.path)
        calls = []
        real = os.replace

        def spy(source, target):
            calls.append((source, target, os.path.exists(source)))
            return real(source, target)

        with patch('bleo.relay_state.os.replace', spy):
            pool.add('https://a.example.com', auth='s')
        (source, target, existed), = calls
        self.assertEqual((os.path.dirname(source), target, existed), (self.dir, self.path, True))
        self.assertFalse(os.path.exists(source))
        self.assertEqual(os.listdir(self.dir), ['relays.json'])

    def test_failed_replace_leaves_the_old_file_whole_and_no_litter(self):
        pool = RelayPool(self.path)
        pool.add('https://a.example.com', auth='s')
        before = self.read()
        with patch('bleo.relay_state.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                pool.add('https://b.example.com', auth='s')
        self.assertEqual(self.read(), before)
        self.assertEqual(sorted(os.listdir(self.dir)), ['relays.json'])

    def test_lock_file_is_released(self):
        RelayPool(self.path).add('https://a.example.com', auth='s')
        self.assertFalse(os.path.exists(self.path + '.lock'))

    def test_fresh_lock_is_not_broken_and_save_still_happens(self):
        open(self.path + '.lock', 'w').close()
        RelayPool(self.path).add('https://a.example.com', auth='s')
        self.assertTrue(os.path.exists(self.path + '.lock'))
        self.assertEqual(len(RelayPool.load(self.path).entries), 1)

    def test_stale_lock_is_taken_over_and_removed(self):
        lock = self.path + '.lock'
        open(lock, 'w').close()
        old = os.stat(lock).st_mtime - 31
        os.utime(lock, (old, old))
        RelayPool(self.path).add('https://a.example.com', auth='s')
        self.assertFalse(os.path.exists(lock))

    def test_lock_is_created_exclusively(self):
        seen = []
        real = os.open

        def spy(path, flags, *args):
            if str(path).endswith('.lock'):
                seen.append(flags & (os.O_CREAT | os.O_EXCL))
            return real(path, flags, *args)

        with patch('bleo.relay_state.os.open', spy):
            RelayPool(self.path).add('https://a.example.com', auth='s')
        self.assertEqual(seen, [os.O_CREAT | os.O_EXCL])

    def test_reloads_when_the_file_changes(self):
        mine = RelayPool(self.path)
        mine.add('https://a.example.com', auth='s')
        other = RelayPool.load(self.path)
        other.add('https://b.example.com', auth='s')
        self.assertEqual([e.label for e in mine.entries], ['a', 'b'])

    def test_does_not_reload_while_the_file_is_unchanged(self):
        pool = RelayPool(self.path)
        pool.add('https://a.example.com', auth='s')
        with patch.object(RelayPool, '_reload') as reload:
            pool.entries, pool.mode, pool.sticky_url
        reload.assert_not_called()

    def test_add_merges_with_a_concurrent_writers_entries(self):
        mine = RelayPool(self.path)
        mine.add('https://a.example.com', auth='s')
        other = RelayPool.load(self.path)
        other.add('https://b.example.com', auth='s')
        mine.add('https://c.example.com', auth='s')
        self.assertEqual([e.label for e in RelayPool.load(self.path).entries], ['a', 'b', 'c'])

    def test_corrupt_file_is_ignored_not_fatal(self):
        with open(self.path, 'w') as handle:
            handle.write('{not json')
        pool = RelayPool.load(self.path)
        self.assertEqual(pool.entries, ())
        pool.add('https://a.example.com', auth='s')
        self.assertEqual(len(RelayPool.load(self.path).entries), 1)

    def test_hand_edited_entries_are_narrowed(self):
        with open(self.path, 'w') as handle:
            json.dump({'relays': [5, {'url': 7}, {'url': ' https://a.example.com '}, {'label': 'x'}],
                       'mode': 'weird'}, handle)
        pool = RelayPool.load(self.path)
        self.assertEqual([e.url for e in pool.entries], ['https://a.example.com'])
        self.assertEqual(pool.mode, 'auto')

    def test_default_path_uses_bleo_home(self):
        with patch.dict(os.environ, {'BLEO_HOME': '/tmp/somewhere'}):
            self.assertEqual(default_path(), '/tmp/somewhere/relays.json')
        with patch.dict(os.environ, {}, clear=True), patch('os.path.expanduser', return_value='/home/me'):
            self.assertEqual(default_path(), '/home/me/.bleo/relays.json')

    def test_load_without_path_reads_the_default(self):
        with patch.dict(os.environ, {'BLEO_HOME': self.dir}):
            RelayPool.load().add('https://a.example.com', auth='s')
            self.assertTrue(os.path.exists(self.path))

    def test_secret_is_not_in_repr(self):
        self.assertNotIn('secret', repr(entry('a')))


class PoolEditTests(PoolCase):
    def test_labels_default_to_the_first_host_label_and_stay_unique(self):
        pool = RelayPool(self.path)
        first = pool.add('https://relay.one.example.com', auth='s')
        second = pool.add('https://relay.two.example.com', auth='s')
        self.assertEqual((first.label, second.label), ('relay', 'relay-2'))

    def test_duplicate_label_rejected_case_insensitively(self):
        pool = RelayPool(self.path)
        pool.add('https://a.example.com', auth='s', label='Alpha')
        with self.assertRaises(ValueError):
            pool.add('https://b.example.com', auth='s', label='alpha')

    def test_adding_the_same_url_updates_it(self):
        pool = RelayPool(self.path)
        pool.add('https://a.example.com', auth='old', label='a1')
        pool.add('https://a.example.com', auth='new')
        self.assertEqual([(e.label, e.auth) for e in pool.entries], [('a1', 'new')])

    def test_remove_by_label_or_url_and_sticky_falls_back(self):
        pool = RelayPool(self.path)
        for name in 'abc':
            pool.add(f'https://{name}.example.com', auth='s')
        pool.set_sticky('https://b.example.com')
        self.assertEqual(pool.remove('B').label, 'b')
        self.assertEqual(pool.sticky_url, 'https://a.example.com')
        self.assertEqual(pool.remove('https://c.example.com').label, 'c')
        self.assertIsNone(pool.remove('nope'))

    def test_set_sticky_rejects_a_stranger(self):
        pool = RelayPool(self.path)
        pool.add('https://a.example.com', auth='s')
        with self.assertRaises(ValueError):
            pool.set_sticky('https://z.example.com')

    def test_entries_is_a_tuple(self):
        self.assertIsInstance(RelayPool(self.path).entries, tuple)

    def test_entry_is_frozen(self):
        with self.assertRaises(Exception):
            entry('a').url = 'https://other.example.com'


class ValidationTests(unittest.TestCase):
    def reject(self, url):
        with self.assertRaises(ValueError, msg=url):
            validate_relay_url(url)

    def test_accepts_a_public_https_url(self):
        for url in ('https://relay.example.com', 'https://relay.example.com:443/path', 'https://my.vercel.app',
                    ' https://relay.example.com '):
            self.assertEqual(validate_relay_url(url), url.strip())

    def test_rejects_http_and_other_schemes(self):
        for url in ('http://relay.example.com', 'ftp://relay.example.com', 'relay.example.com', '//relay.example.com', ''):
            self.reject(url)

    def test_rejects_userinfo(self):
        for url in ('https://user:pw@relay.example.com', 'https://user@relay.example.com'):
            self.reject(url)

    def test_rejects_non_443_ports(self):
        for url in ('https://relay.example.com:8443', 'https://relay.example.com:80', 'https://relay.example.com:99999'):
            self.reject(url)

    def test_rejects_private_loopback_and_link_local_literals(self):
        for host in ('127.0.0.1', '127.1.2.3', '10.0.0.5', '172.16.0.1', '172.31.255.255', '192.168.1.1',
                     '169.254.169.254', '0.0.0.0', '100.64.0.1', '[::1]', '[::]', '[fe80::1]', '[fd00::1]',
                     '[::ffff:127.0.0.1]', '[::ffff:10.0.0.1]'):
            self.reject(f'https://{host}')

    def test_rejects_private_names(self):
        for host in ('localhost', 'LOCALHOST', 'a.localhost', 'printer.local', 'db.internal', 'localhost.'):
            self.reject(f'https://{host}')

    def test_rejects_alternate_ip_spellings(self):
        for host in ('2130706433', '0x7f.0.0.1', '0177.0.0.1', '127.1'):
            self.reject(f'https://{host}')

    def test_accepts_a_public_literal(self):
        self.assertEqual(validate_relay_url('https://8.8.8.8'), 'https://8.8.8.8')

    def test_does_no_dns(self):
        with patch('socket.getaddrinfo', side_effect=AssertionError('resolved')):
            validate_relay_url('https://relay.example.com')

    def test_add_applies_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = RelayPool(os.path.join(directory, 'r.json'))
            for url in ('http://relay.example.com', 'https://10.0.0.1', 'https://u@relay.example.com',
                        'https://relay.example.com:8443'):
                with self.assertRaises(ValueError):
                    pool.add(url, auth='s')
            self.assertFalse(os.path.exists(os.path.join(directory, 'r.json')))

    def test_insecure_flag_is_test_only_and_loosens_everything(self):
        self.assertEqual(validate_relay_url('http://127.0.0.1:8080', allow_insecure=True), 'http://127.0.0.1:8080')
        with self.assertRaises(ValueError):
            validate_relay_url('http://user@127.0.0.1', allow_insecure=True)


if __name__ == '__main__':
    unittest.main()
