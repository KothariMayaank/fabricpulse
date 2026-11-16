| scenario | policy | MFU % | loss vs ideal % | iter p50 ms | iter p99 ms | reroutes | detected online | detect ms | attributed offline | inference p99 slowdown | cost of loss $M/yr |
|---|---|---|---|---|---|---|---|---|---|---|---|
| healthy | ecmp | 22.7 | 44.9 | 147.3 | 147.3 | 0 | - | - | - | - | 98.3 |
| healthy | dlb | 23.7 | 42.4 | 139.8 | 139.8 | 0 | - | - | - | - | 92.9 |
| healthy | telemetry | 32.2 | 22.0 | 102.6 | 102.6 | 556 | - | - | - | - | 48.2 |
| gray_link | ecmp | 18.2 | 55.9 | 196.7 | 197.0 | 0 | 0/3 | - | 1/3 | - | 122.3 |
| gray_link | dlb | 18.6 | 54.8 | 183.3 | 183.3 | 0 | 0/3 | - | 2/3 | - | 120.0 |
| gray_link | telemetry | 32.2 | 22.1 | 102.6 | 103.3 | 501 | 3/3 | 33.8 | 0/3 | - | 48.3 |
| flapping_link | ecmp | 22.8 | 44.7 | 147.3 | 149.3 | 23 | 0/3 | - | 1/3 | - | 98.0 |
| flapping_link | dlb | 23.7 | 42.4 | 139.8 | 140.1 | 4 | 0/3 | - | 0/3 | - | 93.0 |
| flapping_link | telemetry | 32.2 | 22.0 | 102.6 | 103.0 | 463 | 3/3 | 251.0 | 1/3 | - | 48.3 |
| bad_nic | ecmp | 20.7 | 49.8 | 160.3 | 160.3 | 0 | 0/3 | - | 3/3 | - | 109.1 |
| bad_nic | dlb | 22.9 | 44.5 | 145.0 | 145.0 | 0 | 0/3 | - | 3/3 | - | 97.5 |
| bad_nic | telemetry | 31.9 | 22.6 | 102.6 | 118.8 | 525 | 3/3 | 33.8 | 0/3 | - | 49.5 |
| noisy_neighbor | ecmp | 22.3 | 45.8 | 148.0 | 157.4 | 0 | - | - | - | 4.90 | 100.4 |
| noisy_neighbor | dlb | 23.4 | 43.3 | 139.8 | 154.2 | 0 | - | - | - | 4.84 | 94.9 |
| noisy_neighbor | telemetry | 31.9 | 22.6 | 103.2 | 106.1 | 623 | - | - | - | 3.26 | 49.6 |
