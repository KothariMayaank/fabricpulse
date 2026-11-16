| scenario | policy | MFU % | loss vs ideal % | iter p50 ms | iter p99 ms | reroutes | detected online | detect ms | attributed offline | inference p99 slowdown | cost of loss $M/yr |
|---|---|---|---|---|---|---|---|---|---|---|---|
| healthy | ecmp | 32.6 | 21.0 | 102.0 | 102.0 | 0 | - | - | - | - | 45.9 |
| healthy | dlb | 33.7 | 18.4 | 98.3 | 98.3 | 0 | - | - | - | - | 40.3 |
| healthy | telemetry | 41.2 | 0.0 | 80.0 | 80.0 | 0 | - | - | - | - | 0.0 |
| gray_link | ecmp | 25.4 | 38.4 | 134.5 | 134.5 | 0 | 0/3 | - | 1/3 | - | 84.1 |
| gray_link | dlb | 28.4 | 31.2 | 118.4 | 118.4 | 0 | 0/3 | - | 2/3 | - | 68.4 |
| gray_link | telemetry | 41.2 | 0.0 | 80.0 | 80.3 | 3 | 3/3 | 24.4 | 0/3 | - | 0.0 |
| flapping_link | ecmp | 32.1 | 22.2 | 102.0 | 115.5 | 24 | 0/3 | - | 3/3 | - | 48.7 |
| flapping_link | dlb | 33.7 | 18.4 | 98.3 | 98.3 | 4 | 0/3 | - | 0/3 | - | 40.3 |
| flapping_link | telemetry | 41.2 | 0.0 | 80.0 | 80.3 | 1 | 3/3 | 251.0 | 0/3 | - | 0.0 |
| bad_nic | ecmp | 28.0 | 32.0 | 118.5 | 118.5 | 0 | 0/3 | - | 3/3 | - | 70.2 |
| bad_nic | dlb | 29.2 | 29.2 | 113.8 | 113.8 | 0 | 0/3 | - | 3/3 | - | 63.9 |
| bad_nic | telemetry | 40.9 | 0.8 | 80.0 | 96.1 | 0 | 3/3 | 24.4 | 3/3 | - | 1.8 |
| noisy_neighbor | ecmp | 32.3 | 21.6 | 102.0 | 107.2 | 0 | - | - | - | 3.17 | 47.4 |
| noisy_neighbor | dlb | 33.6 | 18.6 | 98.3 | 102.8 | 0 | - | - | - | 2.88 | 40.6 |
| noisy_neighbor | telemetry | 41.1 | 0.3 | 80.0 | 82.7 | 15 | - | - | - | 2.63 | 0.7 |
