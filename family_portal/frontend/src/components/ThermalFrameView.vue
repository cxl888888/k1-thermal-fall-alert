<script setup lang="ts">
import { ref, watchEffect } from 'vue'
const props = defineProps<{ pixels: number[]; low: number; high: number }>()
const canvas = ref<HTMLCanvasElement | null>(null)
const colors = [[32, 28, 60], [65, 64, 131], [148, 59, 110], [233, 105, 64], [255, 230, 154]]

watchEffect(() => {
  const context = canvas.value?.getContext('2d')
  if (!context) return
  const image = context.createImageData(32, 24)
  const span = Math.max(1, props.high - props.low)
  props.pixels.forEach((value, index) => {
    const color = Math.min(1, Math.max(0, (value - props.low) / span)) * (colors.length - 1)
    const base = Math.min(colors.length - 2, Math.floor(color))
    for (let channel = 0; channel < 3; channel++) {
      image.data[index * 4 + channel] = colors[base][channel]
        + (colors[base + 1][channel] - colors[base][channel]) * (color - base)
    }
    image.data[index * 4 + 3] = 255
  })
  context.putImageData(image, 0, 0)
})
</script>

<template>
  <div class="thermal-frame">
    <canvas ref="canvas" width="32" height="24" role="img" aria-label="实时热像画面"></canvas>
  </div>
</template>
