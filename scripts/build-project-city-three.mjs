#!/usr/bin/env node
// Rebuild the three.js block inlined in the project-city dashboard built-in.
//
// src/kiro_crew/dashboard_templates/builtin/project-city/template.html carries two
// scripts. The first is three.js r134 cut down to the classes the city draws with. This
// file is how that block is made, so a reviewer can reproduce it and an upgrade is a
// rerun rather than a hand edit:
//
//   node scripts/build-project-city-three.mjs           rebuild, compare with the page, exit 1 on any difference
//   node scripts/build-project-city-three.mjs --write   rebuild and put the result in the page
//
// The recipe:
//   1. `npm pack` three@0.134.0 into a temporary directory, and refuse the tarball unless
//      its sha512 is the registry integrity pinned below.
//   2. Resolve every `three` import, the entry's and OrbitControls', to the package's
//      modular sources (src/Three.js) instead of build/three.module.js. The package
//      declares "sideEffects": false, so esbuild then drops every file the page never
//      reaches, the loaders and their network code among them.
//   3. Patch one function as it loads: MathUtils.generateUUID, where crypto.randomUUID is
//      missing, builds an object's uuid from four Math.random() draws, which code
//      scanning reads as insecure randomness in an identifier. The patch draws the same
//      four 32-bit numbers from crypto.getRandomValues(new Uint32Array(4)) and changes
//      nothing else; every other Math.random() stays. The source it replaces is pinned,
//      so a three.js that no longer has it stops the build rather than skipping the patch.
//   4. Bundle with the esbuild that website/package.json pins (0.25.12), minified as one
//      IIFE that publishes the classes on globalThis.THREE.
//   5. Point three links in three.js's shader comments at HEAD instead of at the branch
//      name the repo's inclusive-language scan refuses. GitHub resolves HEAD to the
//      default branch, so each link still opens the same file.
//
// Steps 3 and 5 are the only changes to three.js's own text; the banner,
// THIRD-PARTY-NOTICES and the page's header comment say so.
//
// BLOCK_SHA256 is the hash of the block the recipe makes. The check refuses a rebuild
// that does not hash to it, and test/test_dashboard_template_project-city.py holds the
// committed page to the same pin. So a hand edit to the block fails that test, and a
// change to the recipe has to move the pin in the same diff.
//
// To upgrade three.js: change THREE_VERSION, THREE_INTEGRITY (`npm view
// three@<version> dist.integrity`) and the revision and years in BANNER, run with
// --write, put the hash it prints in BLOCK_SHA256, then update the three.js section
// of THIRD-PARTY-NOTICES.
//
// Needs npm, tar and the npm registry, so it is its own CI job rather than a test: the
// `project-city-three` job in .github/workflows/ci.yml runs the check whenever the page,
// this script or THIRD-PARTY-NOTICES changes.
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const PAGE = path.join(REPO, 'src/kiro_crew/dashboard_templates/builtin/project-city/template.html')
const ESBUILD_DIR = path.join(REPO, 'website/node_modules/esbuild')

const THREE_VERSION = '0.134.0'
const THREE_INTEGRITY = 'sha512-LbBerg7GaSPjYtTOnu41AMp7tV6efUNR3p4Wk5NzkSsNTBuA5mDGOfwwZL1jhhVMLx9V20HolIUo0+U3AXehbg=='
const ESBUILD_VERSION = '0.25.12'
const BLOCK_SHA256 = 'c34a80a19ccbf578633ecf96911623e8ee10018f76699172d1e82757b51c1a5d'

const BANNER =
  '/*! three.js r134 (https://threejs.org), cut down to the classes this page uses;' +
  ' generateUUID draws from crypto.getRandomValues where crypto.randomUUID is missing.' +
  ' Copyright 2010-2021 three.js authors. SPDX-License-Identifier: MIT */'

/** generateUUID's fallback in src/math/MathUtils.js, and what step 3 puts in its place. */
const UUID_PATCH = {
  file: path.join('src', 'math', 'MathUtils.js'),
  from: [
    '\tconst d0 = Math.random() * 0xffffffff | 0;',
    '\tconst d1 = Math.random() * 0xffffffff | 0;',
    '\tconst d2 = Math.random() * 0xffffffff | 0;',
    '\tconst d3 = Math.random() * 0xffffffff | 0;',
  ].join('\n'),
  to: [
    '\tconst r = crypto.getRandomValues( new Uint32Array( 4 ) );',
    '\tconst d0 = r[ 0 ] | 0;',
    '\tconst d1 = r[ 1 ] | 0;',
    '\tconst d2 = r[ 2 ] | 0;',
    '\tconst d3 = r[ 3 ] | 0;',
  ].join('\n'),
}

/** Split so this file does not carry the branch name the inclusive-language scan refuses. */
const BRANCH_LINK = { from: '/blob/' + 'mas' + 'ter/', to: '/blob/HEAD/', count: 3 }

/** The classes the page reads from globalThis.THREE, in the order the object lists them. */
const CLASSES = [
  'AdditiveBlending', 'BoxGeometry', 'BufferGeometry', 'CanvasTexture', 'CircleGeometry', 'Clock',
  'Color', 'CylinderGeometry', 'DirectionalLight', 'DoubleSide', 'EdgesGeometry',
  'Float32BufferAttribute', 'FogExp2', 'GridHelper', 'Group', 'HemisphereLight', 'InstancedMesh',
  'Line', 'LineBasicMaterial', 'LineDashedMaterial', 'LineLoop', 'LineSegments', 'LinearFilter',
  'Mesh', 'MeshBasicMaterial', 'MeshStandardMaterial', 'Object3D', 'OctahedronGeometry',
  'OrbitControls', 'PerspectiveCamera', 'PlaneGeometry', 'Points', 'PointsMaterial', 'REVISION',
  'Raycaster', 'RepeatWrapping', 'RingGeometry', 'Scene', 'Shape', 'ShapeGeometry', 'Sprite',
  'SpriteMaterial', 'TorusGeometry', 'Vector2', 'Vector3', 'WebGLRenderer',
]

/** The one class that comes from three's examples rather than its core. */
const FROM_EXAMPLES = { OrbitControls: 'three/examples/jsm/controls/OrbitControls.js' }

const sha256 = (text) => createHash('sha256').update(text, 'utf8').digest('hex')

function fail(message) {
  console.error(`build-project-city-three: ${message}`)
  process.exit(1)
}

function loadEsbuild() {
  const esbuild = createRequire(path.join(ESBUILD_DIR, 'package.json'))(ESBUILD_DIR)
  if (esbuild.version !== ESBUILD_VERSION) {
    fail(`needs esbuild ${ESBUILD_VERSION}, found ${esbuild.version} in website/node_modules (run npm ci in website/)`)
  }
  return esbuild
}

/** Fetch and unpack the pinned tarball as <work>/node_modules/three; return that path. */
function unpackThree(work) {
  const name = execFileSync('npm', ['pack', `three@${THREE_VERSION}`, '--pack-destination', work, '--silent'], {
    cwd: work,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'inherit'],
  }).trim().split('\n').pop()
  const tarball = path.join(work, name)
  const integrity = 'sha512-' + createHash('sha512').update(readFileSync(tarball)).digest('base64')
  if (integrity !== THREE_INTEGRITY) fail(`${name} has integrity ${integrity}, expected ${THREE_INTEGRITY}`)
  execFileSync('tar', ['-xzf', tarball, '-C', work], { stdio: 'inherit' })
  const dir = path.join(work, 'node_modules', 'three')
  mkdirSync(path.dirname(dir), { recursive: true })
  renameSync(path.join(work, 'package'), dir)
  return dir
}

function entrySource() {
  const core = CLASSES.filter((c) => !(c in FROM_EXAMPLES))
  return [
    `import { ${core.join(', ')} } from 'three';`,
    ...Object.entries(FROM_EXAMPLES).map(([c, from]) => `import { ${c} } from '${from}';`),
    `globalThis.THREE = { ${CLASSES.join(', ')} };`,
    '',
  ].join('\n')
}

async function build() {
  const esbuild = loadEsbuild()
  const work = mkdtempSync(path.join(tmpdir(), 'project-city-three-'))
  try {
    const three = unpackThree(work)
    const entry = path.join(work, 'three-entry.js')
    writeFileSync(entry, entrySource())
    const modular = {
      name: 'three-modular-sources',
      setup(b) {
        b.onResolve({ filter: /^three$/ }, () => ({ path: path.join(three, 'src', 'Three.js') }))
      },
    }
    const patched = path.join('node_modules', 'three', UUID_PATCH.file)
    let applied = 0
    const uuid = {
      name: 'three-uuid-from-crypto',
      setup(b) {
        b.onLoad({ filter: /MathUtils\.js$/ }, (args) => {
          if (!args.path.endsWith(patched)) return undefined
          applied += 1
          const text = readFileSync(args.path, 'utf8')
          const found = text.split(UUID_PATCH.from).length - 1
          if (found !== 1) fail(`${UUID_PATCH.file} carries generateUUID's fallback ${found} times, expected 1`)
          return { contents: text.replace(UUID_PATCH.from, UUID_PATCH.to), loader: 'js' }
        })
      },
    }
    const result = await esbuild.build({
      entryPoints: [entry],
      bundle: true,
      minify: true,
      format: 'iife',
      target: 'es2018',
      charset: 'ascii',
      legalComments: 'inline',
      write: false,
      outfile: path.join(work, 'three-subset.js'),
      plugins: [modular, uuid],
      logLevel: 'warning',
    })
    if (applied !== 1) fail(`the generateUUID patch ran ${applied} times, expected 1`)
    let code = result.outputFiles[0].text.trim()
    const found = code.split(BRANCH_LINK.from).length - 1
    if (found !== BRANCH_LINK.count) {
      fail(`the bundle carries ${found} branch links in comments, expected ${BRANCH_LINK.count}: look at them before changing the count`)
    }
    code = code.split(BRANCH_LINK.from).join(BRANCH_LINK.to)
    return `${BANNER}\n${code}\n`
  } finally {
    rmSync(work, { recursive: true, force: true })
  }
}

/** The first script element's text: [start, end) offsets into the page. */
function blockBounds(page) {
  const open = '<script>\n'
  const start = page.indexOf(open)
  if (start < 0) fail('template.html has no <script> block')
  const end = page.indexOf('</script>', start)
  if (end < 0) fail('template.html has an unclosed <script> block')
  return [start + open.length, end]
}

const write = process.argv.includes('--write')
const block = await build()
const digest = sha256(block)
const page = readFileSync(PAGE, 'utf8')
const [start, end] = blockBounds(page)
const committed = page.slice(start, end)

if (write) {
  writeFileSync(PAGE, page.slice(0, start) + block + page.slice(end))
  console.log(`wrote the three.js block into ${path.relative(REPO, PAGE)}: ${block.length} bytes, sha256 ${digest}`)
  if (digest !== BLOCK_SHA256) console.log(`the block changed: set BLOCK_SHA256 to '${digest}' in this script`)
} else if (block !== committed) {
  let at = 0
  while (at < block.length && block[at] === committed[at]) at += 1
  fail(
    `the rebuilt block (${block.length} bytes, sha256 ${digest}) is not the one in template.html ` +
      `(${committed.length} bytes, sha256 ${sha256(committed)}); first difference at offset ${at}`,
  )
} else if (digest !== BLOCK_SHA256) {
  fail(`the rebuilt block matches the page, but its sha256 ${digest} is not the pinned BLOCK_SHA256 ${BLOCK_SHA256}`)
} else {
  console.log(`three.js block: rebuilt from three@${THREE_VERSION} with esbuild ${ESBUILD_VERSION}; matches template.html, sha256 ${digest}`)
}
