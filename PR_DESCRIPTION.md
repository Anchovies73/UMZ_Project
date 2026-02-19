# Fix selection overlay transforms and logical-node bounds

## Changes

### 1. Fixed shift+click logical-node traversal boundary
- Changed `findNearestLogicalNode` to stop at `modelRoot` instead of `modelRoot.parent`
- This prevents selection from traversing outside the loaded model

### 2. Made selection overlay transform updates robust for animated meshes
- Call `sourceMesh.updateWorldMatrix(false, false)` before copying transforms (updateParents=false since mixer already updates parent transforms)
- Copy transforms to `overlay.matrix` (since `matrixAutoUpdate=false`)
- Also set `overlay.matrixWorld` for consistency
- Set `overlay.matrixWorldNeedsUpdate = false` after update

### 3. Reuse single shared overlay material
- Created `sharedOverlayMaterial` at module level
- Modified `createOverlayMesh` to use shared material instead of creating new ones
- Updated `clearSelectionFill` to not dispose the shared material

## Files Modified
- `web/main.js`
