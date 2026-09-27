#!/bin/bash

set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

run_node_test <<'JS'
const fs = require('fs')

const shellQml = fs.readFileSync(path.join(root, 'shell/shell.qml'), 'utf8')
const menuQml = fs.readFileSync(path.join(root, 'shell/plugins/menu/Menu.qml'), 'utf8')

// Exercise the production JavaScript body rather than duplicating the policy
// in a test helper. This keeps a future allowlist edit covered for positive,
// negative, first-party, and non-minimal cases.
const policyMatch = shellQml.match(/function piMinimalServiceAllowed\(pluginId, manifest\) \{([\s\S]*?)\n  \}/)
assert(policyMatch, 'Pi service allowlist function is present')
const serviceAllowed = new Function('piMinimalSession', 'pluginId', 'manifest', policyMatch[1])

assertEqual(
  serviceAllowed(true, 'omarchy.background', { __isFirstParty: true }),
  true,
  'Pi profile admits the first-party background service'
)
assertEqual(
  serviceAllowed(true, 'omarchy.notifications', { __isFirstParty: true }),
  true,
  'Pi profile admits the first-party notification service'
)
assertEqual(
  serviceAllowed(true, 'omarchy.media', { __isFirstParty: true }),
  true,
  'Pi profile admits the first-party media service'
)
assertEqual(
  serviceAllowed(true, 'omarchy.nightlight', { __isFirstParty: true }),
  true,
  'Pi profile admits the first-party nightlight service'
)
assertEqual(
  serviceAllowed(true, 'omarchy.polkit', { __isFirstParty: true }),
  true,
  'Pi profile admits the first-party polkit service'
)
for (const id of ['omarchy.idle', 'omarchy.lock']) {
  assertEqual(
    serviceAllowed(true, id, { __isFirstParty: true }),
    true,
    'Pi profile admits the first-party ' + id + ' service'
  )
}
assertEqual(
  serviceAllowed(true, 'omarchy.notifications', { __isFirstParty: false }),
  false,
  'Pi profile does not admit a third-party plugin under the notification id'
)
assertEqual(
  serviceAllowed(true, 'omarchy.background', { __isFirstParty: false }),
  false,
  'Pi profile does not admit a third-party plugin under the background id'
)
assertEqual(
  serviceAllowed(false, 'acme.service', { __isFirstParty: false }),
  true,
  'full profiles retain the normal service policy'
)

const fallbackMatch = shellQml.match(/readonly property var piMinimalShellConfig: \(\{([\s\S]*?)\n  \}\)/)
assert(fallbackMatch, 'Pi fallback shell configuration is present')
assert(
  !fallbackMatch[1].includes('"omarchy.notifications"'),
  'Pi fallback shell configuration does not disable notifications'
)
for (const id of ['omarchy.media', 'omarchy.nightlight', 'omarchy.polkit']) {
  assert(!fallbackMatch[1].includes('"' + id + '"'), 'Pi fallback shell configuration does not disable ' + id)
}
for (const id of ['omarchy.idle', 'omarchy.lock']) {
  assert(!fallbackMatch[1].includes('"' + id + '"'), 'Pi fallback shell configuration does not disable ' + id)
}
for (const id of [
  'omarchy.indicators',
  'omarchy.clock',
  'omarchy.keyboard-layout',
  'omarchy.tray',
  'omarchy.bluetooth',
  'omarchy.network',
  'omarchy.audio'
]) {
  assert(fallbackMatch[1].includes('id: "' + id + '"'),
    'Pi fallback bar retains ' + id)
}

assert(
  menuQml.includes('readonly property bool piMinimalSession: Quickshell.env("OMARCHY_PI_MINIMAL_SESSION") === "1"'),
  'launcher reads the bounded Pi session flag'
)
assert(
  /function goBack\(\) \{[\s\S]*root\.piMinimalSession && root\.activeMenu === "apps" && root\.navStack\.length === 0[\s\S]*return false/.test(menuQml),
  'direct Pi Apps launcher navigation does not expose the broad root menu'
)
JS

pass "Pi service allowlist covers the Pi desktop services"
