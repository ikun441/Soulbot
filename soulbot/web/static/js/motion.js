// Native, interruptible motion. No animation framework or build step required.
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const ease = 'cubic-bezier(.16,1,.3,1)';
let transition, observer, lastNavRect;
export const motionEnabled = () => document.body.dataset.motion === 'true' && !reducedMotion.matches;

function play(element, frames, options = {}) {
  if (!element || !motionEnabled() || !element.animate) return null;
  return element.animate(frames, {duration: 420, easing: ease, ...options});
}

export function stopMotion() {
  transition?.skipTransition();
  observer?.disconnect();
  document.getAnimations().forEach(animation => animation.cancel());
}

export function changePage(update, direction = 'forward') {
  if (!motionEnabled() || !document.startViewTransition) { update(); return; }
  transition?.skipTransition();
  document.documentElement.dataset.direction = direction;
  // The callback owns the DOM update and event binding together.
  const current = document.startViewTransition(update);
  transition = current;
  current.ready.catch(() => {});
  current.finished.catch(() => {}).finally(() => {if (transition === current) transition = null;});
}

export function revealPage(main) {
  observer?.disconnect();
  if (!motionEnabled()) return;
  const parts = main.querySelectorAll('.intro > *, .wizard-title, .wizard-steps, .wizard-body > *, main > .panel, .metric, .two-columns > .panel, .wizard-footer');
  let index = 0;
  for (const part of parts) {
    if (part.getBoundingClientRect().top < innerHeight + 30) {
      play(part, [{opacity:0, transform:'translateY(16px)'}, {opacity:1, transform:'translateY(0)'}], {delay:Math.min(index++ * 32, 180), duration:460});
    }
  }
  observer = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) {
      play(entry.target, [{opacity:0, transform:'translateY(22px)'}, {opacity:1, transform:'translateY(0)'}], {duration:560});
      observer.unobserve(entry.target);
    }
  }, {threshold:.08});
  main.querySelectorAll('.two-columns > .panel, .metric').forEach(card => {
    if (card.getBoundingClientRect().top >= innerHeight + 30) observer.observe(card);
  });
  main.querySelectorAll('details').forEach(details => details.addEventListener('toggle', () => {
    if (details.open) play(details.lastElementChild, [{opacity:0, transform:'translateY(-8px)'}, {opacity:1, transform:'translateY(0)'}], {duration:240});
  }));
}

export function moveNav(nav) {
  nav.querySelector(".nav-marker")?.remove();
  const active = nav.querySelector('[aria-current="page"]');
  if (!active) {lastNavRect = null; return;}
  const rect = active.getBoundingClientRect();
  const marker = document.createElement('span');
  marker.className = 'nav-marker';marker.setAttribute('aria-hidden','true');
  Object.assign(marker.style, {width:rect.width+'px',height:rect.height+'px',left:(active.offsetLeft)+'px',top:(active.offsetTop)+'px'});
  nav.prepend(marker);
  if (lastNavRect) play(marker, [
    {transform:`translate(${lastNavRect.left-rect.left}px, ${lastNavRect.top-rect.top}px) scale(${lastNavRect.width/rect.width},${lastNavRect.height/rect.height})`},
    {transform:'translate(0,0) scale(1,1)'}
  ], {duration:420});
  lastNavRect = rect;
}

export function toast(element, show, complete = () => {}) {
  element.getAnimations().forEach(animation => animation.cancel());
  if (show) element.hidden = false;
  const animation = play(element, show ? [
    {opacity:0,transform:'translateY(18px) scale(.96)'},
    {opacity:1,transform:'translateY(-3px) scale(1.01)',offset:.75},
    {opacity:1,transform:'translateY(0) scale(1)'}
  ] : [{opacity:1,transform:'translateY(0)'},{opacity:0,transform:'translateY(12px)'}], {duration:show?340:160});
  if (animation) animation.finished.then(complete).catch(() => {}); else complete();
}

export function openDialog(dialog) {
  dialog.showModal();
  play(dialog,[{opacity:0,transform:'translateY(20px) scale(.95)'},{opacity:1,transform:'translateY(0) scale(1)'}],{duration:320});
}

export function closeDialog(dialog) {
  dialog.getAnimations().forEach(animation=>animation.cancel());
  const animation=play(dialog,[{opacity:1,transform:'translateY(0) scale(1)'},{opacity:0,transform:'translateY(10px) scale(.98)'}],{duration:150});
  if (animation) animation.finished.then(()=>dialog.close()).catch(()=>dialog.close()); else dialog.close();
}

export function wallpaperFade(element, previous = []) {
  const animation = play(element,[{opacity:0,transform:'scale(1.035)'},{opacity:1,transform:'scale(1)'}],{duration:680});
  const cleanup = () => previous.forEach(node=>node.remove());
  if (animation) animation.finished.then(cleanup).catch(cleanup); else cleanup();
}

// Delegate pointer feedback once, so page changes never accumulate listeners.
document.addEventListener('pointerdown', event => {
  const button=event.target.closest('.button');
  if (!button || button.disabled || !motionEnabled()) return;
  const rect=button.getBoundingClientRect();
  const ripple=document.createElement('span');ripple.className='click-ripple';ripple.setAttribute('aria-hidden','true');
  const size=Math.max(rect.width,rect.height)*2;
  Object.assign(ripple.style,{width:size+'px',height:size+'px',left:(event.clientX-rect.left-size/2)+'px',top:(event.clientY-rect.top-size/2)+'px'});
  button.append(ripple);
  const animation=play(ripple,[{opacity:.45,transform:'scale(0)'},{opacity:0,transform:'scale(1)'}],{duration:560});
  if(animation) animation.finished.then(()=>ripple.remove()).catch(()=>ripple.remove()); else ripple.remove();
});
reducedMotion.addEventListener('change',()=>{if(reducedMotion.matches)stopMotion();});

let resizeFrame;
window.addEventListener('resize',()=>{
  cancelAnimationFrame(resizeFrame);
  resizeFrame=requestAnimationFrame(()=>{const nav=document.querySelector('#navigation');if(nav)moveNav(nav);});
});
