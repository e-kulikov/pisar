import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from .support import CHECKOUT, EXECUTABLE, clean_environ, pisar_command, space
from pisar import guard


def scan(text, terms=()):
    return guard.scan(text, terms)


def categories(text, terms=()):
    return [(f['category'], f['excerpt']) for f in scan(text, terms)]


def only(test, text, category, terms=()):
    found = [f for f in scan(text, terms) if f['category'] == category]
    test.assertTrue(found, f'no {category} finding in {text!r}')
    return found


class TierTests(unittest.TestCase):
    def assert_tier(self, text, category, tier, excerpt=None):
        found = only(self, text, category)
        self.assertEqual({f['tier'] for f in found}, {tier})
        if excerpt is not None:
            self.assertIn(excerpt, [f['excerpt'] for f in found])

    def assert_not(self, text, category):
        self.assertEqual([f for f in scan(text) if f['category'] == category], [], text)

    def test_private_key_block_is_severe(self):
        text = ('Before\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n'
                '-----END RSA PRIVATE KEY-----\nAfter\n')
        found = only(self, text, 'private-key')
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]['tier'], found[0]['line']), ('severe', 2))
        self.assertEqual(found[0]['excerpt'], '-----BEGIN RSA PRIVATE KEY-----')
        self.assert_tier('-----BEGIN OPENSSH PRIVATE KEY-----\nb3Blbg==\n', 'private-key', 'severe')
        self.assert_not('-----BEGIN PUBLIC KEY-----\nMIIBIjAN\n-----END PUBLIC KEY-----\n', 'private-key')
        self.assert_not('Rotate the private key every year.', 'private-key')

    def test_cloud_and_api_tokens_are_severe(self):
        for token in ('AKIAIOSFODNN7EXAMPLE', 'ghp_' + 'a1B2' * 9, 'github_pat_' + 'x' * 30,
                      'xoxb-1234567890-abcdefghij', 'AIza' + 'b' * 35, 'sk-ant-' + 'c' * 30,
                      'sk_live_' + 'd' * 24, 'glpat-' + 'e' * 20):
            with self.subTest(token=token):
                self.assert_tier(f'use {token} here', 'api-token', 'severe', token)
        for text in ('The AKIA prefix marks AWS keys.', 'ghp_short', 'task-ant-like words',
                     'AKIAIOSFODNN7EXAMPLEXTRA'):
            with self.subTest(text=text):
                self.assert_not(text, 'api-token')

    def test_jwt_is_severe(self):
        jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U'
        found = only(self, f'Bearer {jwt}', 'jwt')
        self.assertEqual([(f['tier'], f['excerpt']) for f in found], [('severe', jwt[:79] + '…')])
        self.assert_not('eyJ alone is not a token', 'jwt')

    def test_credential_assignments_are_severe(self):
        for text, excerpt in (('password = hunter22', 'password = hunter22'),
                              ('api_key: "abc123def"', 'api_key: "abc123def"'),
                              ('DB_PASSWORD=s3cr3t', 'PASSWORD=s3cr3t'),
                              ('client_secret = xyz789', 'client_secret = xyz789'),
                              ('TOKEN=xyz', 'TOKEN=xyz')):
            with self.subTest(text=text):
                self.assert_tier(text, 'credential', 'severe', excerpt)
        for text in ('Reset the password regularly.', 'The token expired.',
                     'if token == expected:', 'secrets are rotated'):
            with self.subTest(text=text):
                self.assert_not(text, 'credential')

    def test_emails_are_severe_client_data(self):
        self.assert_tier('Write to jane.doe+x@example.com today', 'email', 'severe', 'jane.doe+x@example.com')
        self.assert_tier('Пишите иван@пример.рф', 'email', 'severe', 'иван@пример.рф')
        for text in ('ping @team in chat', 'a@b', 'user@localhost'):
            with self.subTest(text=text):
                self.assert_not(text, 'email')

    def test_phone_numbers_are_severe_client_data(self):
        for phone in ('+1 415 555 0100', '+7 (912) 345-67-89', '+44 20 7946 0958',
                      '(415) 555-0100', '415-555-0100', '+4915112345678'):
            with self.subTest(phone=phone):
                self.assert_tier(f'Call {phone} now.', 'phone', 'severe', phone)

    def test_fenced_code_blocks_are_severe_one_finding_each(self):
        text = 'Intro\n\n```python\nimport os\n```\n\nMiddle\n\n~~~\nplain\n~~~\n'
        found = only(self, text, 'code-block')
        self.assertEqual([(f['tier'], f['line'], f['excerpt']) for f in found],
                         [('severe', 3, '```python'), ('severe', 9, '~~~')])
        unclosed = only(self, 'Text\n````\ncode without end\n', 'code-block')
        self.assertEqual([f['line'] for f in unclosed], [2])
        self.assert_not('Use `inline code` and ``` in the middle of a line.', 'code-block')

    def test_blockquotes_and_long_quoted_strings_are_ask(self):
        text = 'Said:\n> first quoted line\n> second line\n\nGap\n\n> another quote\n'
        found = only(self, text, 'quote')
        self.assertEqual([(f['tier'], f['line'], f['excerpt']) for f in found],
                         [('ask', 2, '> first quoted line'), ('ask', 7, '> another quote')])
        long = '“We will never ship this before the audit is finished”'
        self.assert_tier(f'She said {long}.', 'quote', 'ask', long)
        self.assert_tier('Он сказал «мы не будем выпускать это до конца аудита».', 'quote', 'ask')
        self.assert_tier('He said "we will not ship until the audit is over".', 'quote', 'ask')
        for text in ('Click "Save" then "OK".', 'A > B in this case.', '```\n> not a quote\n```\n',
                     'Set "REPLACE_WITH_ACTUAL_64_CHARACTER_SHA256_VALUE" first.'):
            with self.subTest(text=text):
                self.assert_not(text, 'quote')

    def test_meeting_markers_are_ask(self):
        text = '[00:12:34] Anna Petrova: we agreed\nBob: fine\n00:15 next topic\n'
        found = only(self, text, 'meeting')
        self.assertEqual({f['tier'] for f in found}, {'ask'})
        excerpts = [f['excerpt'] for f in found]
        for expected in ('[00:12:34]', 'Anna Petrova:', 'Bob:', '00:15'):
            self.assertIn(expected, excerpts)
        self.assert_tier('**Мария:** согласна\n', 'meeting', 'ask')
        for text in ('Note: keep it short.', 'Why: because.', 'The ratio is 3:1 here.',
                     'see the docs: they explain', 'How to apply: always.'):
            with self.subTest(text=text):
                self.assert_not(text, 'meeting')

    def test_urls_hostnames_and_ips_are_ask(self):
        self.assert_tier('See https://intranet.acme.example/wiki/page?id=3.', 'url', 'ask',
                         'https://intranet.acme.example/wiki/page?id=3')
        self.assert_tier('Open www.example.org now', 'url', 'ask', 'www.example.org')
        self.assert_tier('Connect to db01.corp.internal first', 'hostname', 'ask', 'db01.corp.internal')
        self.assert_tier('Server 10.0.12.7 is down', 'ip', 'ask', '10.0.12.7')
        self.assert_tier('Subnet 192.168.1.0/24 only', 'ip', 'ask', '192.168.1.0/24')
        self.assert_tier('Link-local fe80::1ff:fe23:4567:890a works', 'ip', 'ask', 'fe80::1ff:fe23:4567:890a')
        for text in ('Edit README.md and config.toml', 'Call os.walk(root)', 'Version 1.2.3 shipped',
                     'Invalid 999.1.1.1', 'Use std::vector here', 'e.g. this, i.e. that'):
            with self.subTest(text=text):
                self.assert_not(text, 'hostname')
                self.assert_not(text, 'ip')

    def test_hosts_inside_urls_and_emails_are_not_reported_twice(self):
        found = categories('Mail ops@mail.example.com or https://10.1.2.3/x and https://api.example.com')
        self.assertEqual(sorted(c for c, _ in found), ['email', 'url', 'url'])

    def test_dates_with_day_or_finer_precision_are_warn(self):
        for date in ('2026-10-07', '2026-10-07T14:30Z', '07.10.2026', '07/10/26', 'October 7, 2026',
                     'Oct 7', '7 October', '13 April 2026', '25 May', '7th of March 2026', '7 октября 2026', '14:30', '3pm'):
            with self.subTest(date=date):
                self.assert_tier(f'Met on {date} again', 'date', 'warn', date)
        for text in ('Planned for October 2026.', 'Back in 2026.', 'Q3 2026 goals', 'Since 2026-10.',
                     'Screens are 16:9.', 'Version 1.2.10'):
            with self.subTest(text=text):
                self.assert_not(text, 'date')

    def test_timezone_offset_is_part_of_the_time(self):
        found = only(self, 'Due 23:59:59+01:00 sharp', 'date')
        self.assertEqual([f['excerpt'] for f in found], ['23:59:59'])

    def test_amounts_and_metrics_are_not_reported(self):
        text = ('Revenue grew to $1,200,000 (+15%) and €5k per month; 3 500 000 руб. budget.\n'
                'p95 latency 230 ms, 99.9% uptime, 1.5x faster, 12.5 million users, 42% churn.\n'
                'Costs fell by 12 345 678 and +12 500 000 subscribers; ratio 3:1; score 4.5/5.\n')
        self.assertEqual(scan(text), [])


class TermTests(unittest.TestCase):
    terms = [('Acme Corp', 'acme: domain title'), ('Acme', 'acme: alias'),
             ('launch-plan', 'acme: space id'), ('Ромашка', 'acme: sensitive term'),
             ('AI', 'acme: alias')]

    def test_terms_are_warn_case_insensitive_on_word_boundaries(self):
        text = 'ACME shipped. acme-tools too. Acmegrams no. The LAUNCH-PLAN moved.'
        found = only(self, text, 'term', self.terms)
        self.assertEqual([(f['tier'], f['excerpt']) for f in found],
                         [('warn', 'ACME'), ('warn', 'acme'), ('warn', 'LAUNCH-PLAN')])
        self.assertIn('acme: alias', found[0]['hint'])

    def test_longest_term_wins_and_whitespace_may_differ(self):
        found = only(self, 'At Acme\n  corp we met.', 'term', self.terms)
        self.assertEqual([(f['excerpt'], f['line']) for f in found], [('Acme', 1)])
        self.assertIn('domain title', found[0]['hint'])

    def test_unicode_terms_and_short_terms(self):
        found = only(self, 'Проект РОМАШКА; ромашки нет. AI and ai are ignored.', 'term', self.terms)
        self.assertEqual([f['excerpt'] for f in found], ['РОМАШКА'])


class IdentityTests(unittest.TestCase):
    def test_finding_id_formula(self):
        [finding] = scan('Mail Jane.Doe@Example.com')
        expected = hashlib.sha256('email|jane.doe@example.com|1'.encode()).hexdigest()[:10]
        self.assertEqual(finding['id'], expected)

    def test_ids_stable_across_runs_and_unrelated_edits(self):
        text = 'Mail a@example.com and a@example.com.\nCall +1 415 555 0100.\n'
        first = scan(text)
        self.assertEqual(first, scan(text))
        ids = [f['id'] for f in first]
        self.assertEqual(len(set(ids)), 3)
        edited = scan('A new unrelated paragraph.\n\n' + text.replace('Call', 'Phone') + 'More 42% text.\n')
        self.assertEqual([f['id'] for f in edited], ids)
        self.assertEqual([f['line'] for f in edited], [3, 3, 4])

    def test_unicode_line_numbers_and_excerpts(self):
        found = scan('Привет, мир 🌍\nЁжик: да\nпочта ёж@пример.рф\n')
        self.assertEqual([(f['category'], f['line'], f['excerpt']) for f in found],
                         [('meeting', 2, 'Ёжик:'), ('email', 3, 'ёж@пример.рф')])

    def test_findings_have_exact_shape_in_document_order(self):
        found = scan('2026-10-07\nhttps://example.org\npassword = x1\n')
        self.assertEqual([f['category'] for f in found], ['date', 'url', 'credential'])
        for f in found:
            self.assertEqual(set(f), {'id', 'tier', 'category', 'line', 'excerpt', 'hint'})
            self.assertRegex(f['id'], r'^[0-9a-f]{10}$')
            self.assertTrue(f['hint'])


def domain(root, ident, title, aliases=(), terms=()):
    path = root / ident
    path.mkdir(parents=True, exist_ok=True)
    (path / '.domain.toml').write_text(
        f'schema_version = 1\nid = "{ident}"\ntitle = {json.dumps(title)}\n'
        f'[sensitive]\naliases = {json.dumps(list(aliases))}\nterms = {json.dumps(list(terms))}\n',
        encoding='utf-8')
    return path


class CommandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='pisar-guard-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'wiki'
        acme = domain(self.root, 'acme', 'Acme Corp', aliases=['ACM Industries'], terms=['Bluebird'])
        launch = space(acme, '10-projects/launch-plan', 'launch-plan')
        (launch / 'README.md').write_text('+++\n# not a title\n+++\n# Orion Rollout\n\nText\n')
        space(acme, '40-archives/old-thing', 'old-thing')
        home = domain(self.root, 'home', 'Household')
        space(home, '20-areas/garden', 'garden', kind='area')
        (self.root / 'templates').mkdir()
        space(self.root, 'templates/sample', 'sample-space')
        self.env = clean_environ(XDG_DATA_HOME=str(self.base / 'data'), XDG_CONFIG_HOME=str(self.base / 'config'))

    def run_guard(self, *args, code=0):
        p = subprocess.run([*pisar_command(), '--root', str(self.root), '--state-dir', str(self.base / 'state'),
                            'guard', *map(str, args)], cwd=self.base if EXECUTABLE else CHECKOUT,
                           env=self.env, text=True, capture_output=True)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        return p

    def check(self, *args):
        return json.loads(self.run_guard('check', *args).stdout)

    def excerpts(self, result, category='term'):
        return [f['excerpt'] for f in result['findings'] if f['category'] == category]

    def test_json_shape_and_exit_zero_with_findings(self):
        result = self.check('--text', 'Acme mail ops@example.com', '--from', 'acme', '--to', 'home')
        self.assertEqual(set(result), {'findings', 'limits'})
        self.assertIn('best effort', result['limits'].lower())
        self.assertEqual([f['category'] for f in result['findings']], ['term', 'email'])
        self.assertEqual(self.check('--text', 'Plain words only.', '--from', 'acme')['findings'], [])

    def test_source_domain_terms_only_unless_outbound(self):
        text = ('Acme Corp, ACM Industries, bluebird, launch-plan, Orion Rollout, old-thing; '
                'Household garden; sample-space; templates.')
        own = self.check('--text', text, '--from', 'acme')
        self.assertEqual(self.excerpts(own), ['Acme Corp', 'ACM Industries', 'bluebird', 'launch-plan',
                                              'Orion Rollout', 'old-thing'])
        other = self.check('--text', text, '--from', 'home')
        self.assertEqual(self.excerpts(other), ['Household', 'garden'])
        outbound = self.check('--text', text, '--outbound')
        self.assertEqual(self.excerpts(outbound), ['Acme Corp', 'ACM Industries', 'bluebird', 'launch-plan',
                                                   'Orion Rollout', 'old-thing', 'Household', 'garden'])
        self.assertEqual(self.check('--text', text, '--outbound', '--from', 'home'), outbound)

    def test_file_input_is_read_and_never_modified(self):
        draft = self.base / 'draft.md'
        original = '# Lesson\r\n\r\nAt Acme we learnt to wait.\r\n```sh\r\nrm -rf build\r\n```\r\n'.encode()
        draft.write_bytes(original)
        before = draft.stat().st_mtime_ns
        result = self.check('--file', draft, '--from', 'acme')
        self.assertEqual([(f['category'], f['line']) for f in result['findings']],
                         [('term', 3), ('code-block', 4)])
        self.assertEqual(draft.read_bytes(), original)
        self.assertEqual(draft.stat().st_mtime_ns, before)

    def test_text_starting_with_dashes_and_ids_stable_between_runs(self):
        text = '--text=-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n'
        first, second = self.check(text, '--from', 'acme'), self.check(text, '--from', 'acme')
        self.assertEqual(first, second)
        self.assertEqual([f['category'] for f in first['findings']], ['private-key'])

    def test_errors_exit_nonzero(self):
        self.assertIn('unknown domain', self.run_guard('check', '--text', 'x', '--from', 'nope', code=1).stderr)
        self.run_guard('check', '--text', 'x', '--from', 'acme', '--to', 'nope', code=1)
        self.run_guard('check', '--text', 'x', code=1)
        self.run_guard('check', '--file', self.base / 'missing.md', '--from', 'acme', code=1)
        (self.base / 'binary.md').write_bytes(b'\xff\xfe\x00')
        self.run_guard('check', '--file', self.base / 'binary.md', '--from', 'acme', code=1)
        self.run_guard('check', '--from', 'acme', code=2)
        self.run_guard('check', '--text', 'x', '--file', self.base / 'x', '--from', 'acme', code=2)

    def test_invalid_domain_markers_are_errors(self):
        marker = self.root / 'home/.domain.toml'
        marker.write_text('schema_version = 1\nid = "other"\n')
        self.assertIn('home', self.run_guard('check', '--text', 'x', '--from', 'acme', code=1).stderr)
        marker.write_text('schema_version = 1\nid = "home"\n[sensitive]\nterms = "garden"\n')
        self.run_guard('check', '--text', 'x', '--from', 'acme', code=1)
        marker.write_text('schema_version = [\n')
        self.run_guard('check', '--text', 'x', '--from', 'acme', code=1)

    def test_symlinked_domains_are_refused(self):
        real = self.base / 'elsewhere'
        domain(self.base, 'elsewhere', 'Elsewhere')
        os.symlink(real, self.root / 'linked')
        self.run_guard('check', '--text', 'x', '--from', 'acme', code=1)
        (self.root / 'linked').unlink()
        (self.root / 'home/.domain.toml').rename(self.base / 'marker.toml')
        os.symlink(self.base / 'marker.toml', self.root / 'home/.domain.toml')
        self.run_guard('check', '--text', 'x', '--from', 'acme', code=1)

    def test_help_states_best_effort_limits(self):
        text = ' '.join(self.run_guard('check', '--help').stdout.split()).lower()
        self.assertIn('best effort', text)
        self.assertIn('never edits', text)
        self.assertIn('amounts and metrics are not reported', text)


if __name__ == '__main__':
    unittest.main()
